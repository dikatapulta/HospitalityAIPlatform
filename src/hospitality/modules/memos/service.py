"""Черновик и переходы записки без подтверждения (spec 0077 §3) — копия канона
`modules/requests/service.py`.

Каждая функция — одна транзакция внутри `tenant_context` (его ставит кабинет):
чужой отель отсекает RLS (P-4). Флаг раздела `memos_enabled` проверяет кабинет
(PR 4), как у «Наставника»: данные модуля от флага не зависят — ретеншн идёт и
у отеля, выключившего раздел. Действия под именем — отправка, подпись, решение —
в `signing.py`; объяснительные и требования — `explanations.py` и `demands.py`.
В логах — id, вид, статус; без текстов, заголовков и имён (§8).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.memos.access import (
    check_participants,
    invalid_input,
    load_visible,
    not_found,
    require_author,
    signer_ids,
)
from hospitality.modules.memos.codec import memo_body, memo_title, seal_memo
from hospitality.modules.memos.models import (
    Memo,
    MemoCounter,
    MemoKind,
    MemoSignature,
    MemoStatus,
    SignatureRole,
)
from hospitality.modules.memos.schemas import MemoDraftInput
from hospitality.platform.config import TenantConfig, load_tenant_config
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)

NUMBER_PREFIX: Final = {MemoKind.MEMO: "СЗ", MemoKind.EXPLANATION: "ОБ"}


async def hotel_config(tenant_id: uuid.UUID) -> TenantConfig:
    async with platform_session_scope() as session:
        return await load_tenant_config(session, tenant_id)


async def next_number(
    session: AsyncSession, tenant_id: uuid.UUID, kind: MemoKind, year: int
) -> str:
    """Следующий номер внутри отеля, вида и года: «СЗ-2026/14» (§3.2).

    Одна инструкция `INSERT … ON CONFLICT DO UPDATE … RETURNING`: вторая отправка
    ждёт строку счётчика первой до её коммита — ни дублей, ни дыр; откат отправки
    откатывает и номер."""
    statement = (
        insert(MemoCounter)
        .values(tenant_id=tenant_id, kind=kind, year=year, last_number=1)
        .on_conflict_do_update(
            index_elements=[MemoCounter.tenant_id, MemoCounter.kind, MemoCounter.year],
            set_={"last_number": MemoCounter.last_number + 1},
        )
        .returning(MemoCounter.last_number)
    )
    number = (await session.execute(statement)).scalar_one()
    return f"{NUMBER_PREFIX[kind]}-{year}/{number}"


def _check_template(config: TenantConfig, template_key: str | None) -> None:
    if template_key is not None and template_key not in {t.key for t in config.memo_templates}:
        raise invalid_input("template_key", "unknown_template")


def _replace_signers(
    session: AsyncSession, memo: Memo, signers: tuple[uuid.UUID, ...]
) -> list[MemoSignature]:
    rows = [
        MemoSignature(
            tenant_id=memo.tenant_id, memo_id=memo.id, user_id=user_id, role=SignatureRole.SIGNER
        )
        for user_id in signers
    ]
    session.add_all(rows)
    return rows


async def create_draft(staff: StaffContext, draft: MemoDraftInput) -> uuid.UUID:
    """Новый черновик записки; автор — сотрудник сессии. Объяснительная — `explanations`."""
    _check_template(await hotel_config(staff.tenant_id), draft.template_key)
    await check_participants(staff, draft.addressee_id, draft.signer_ids)
    async with session_scope() as session:
        memo = Memo(
            id=uuid.uuid4(),
            tenant_id=staff.tenant_id,
            kind=MemoKind.MEMO,
            template_key=draft.template_key,
            author_id=staff.user_id,
            addressee_id=draft.addressee_id,
            status=MemoStatus.DRAFT,
        )
        memo.title_ciphertext = seal_memo(memo, "title_ciphertext", draft.title)
        memo.body_ciphertext = seal_memo(memo, "body_ciphertext", draft.body)
        session.add(memo)
        await session.flush()
        _replace_signers(session, memo, draft.signer_ids)
    logger.info("memo_draft_created", memo_id=str(memo.id), signers=len(draft.signer_ids))
    return memo.id


async def update_draft(staff: StaffContext, memo_id: uuid.UUID, draft: MemoDraftInput) -> None:
    """Правка черновика автором: текст, адресат, подписанты. После отправки — ERR-MEMO-002."""
    _check_template(await hotel_config(staff.tenant_id), draft.template_key)
    await check_participants(staff, draft.addressee_id, draft.signer_ids)
    async with session_scope() as session:
        memo, _ = await load_visible(session, staff, memo_id, lock=True)
        require_author(memo, staff, MemoStatus.DRAFT)
        if memo.kind is not MemoKind.MEMO:  # объяснительная — `update_explanation`
            raise not_found()
        memo.template_key = draft.template_key
        memo.addressee_id = draft.addressee_id
        memo.title_ciphertext = seal_memo(memo, "title_ciphertext", draft.title)
        memo.body_ciphertext = seal_memo(memo, "body_ciphertext", draft.body)
        memo.updated_at = utc_now()
        await session.execute(delete(MemoSignature).where(MemoSignature.memo_id == memo.id))
        _replace_signers(session, memo, draft.signer_ids)
    logger.info("memo_draft_updated", memo_id=str(memo_id), signers=len(draft.signer_ids))


async def withdraw(staff: StaffContext, memo_id: uuid.UUID) -> None:
    """«Отозвать» — автор, пока запись не у адресата (§3.7); номер остаётся занятым."""
    async with session_scope() as session:
        memo, _ = await load_visible(session, staff, memo_id, lock=True)
        require_author(memo, staff, MemoStatus.DRAFT, MemoStatus.SIGNING)
        memo.status = MemoStatus.WITHDRAWN
        memo.withdrawn_at = memo.updated_at = utc_now()
    logger.info("memo_withdrawn", memo_id=str(memo_id))


async def submit_to_addressee(staff: StaffContext, memo_id: uuid.UUID) -> None:
    """«Отправить руководителю» из `signing` (§3.4): лист подписей фиксируется — кто
    не ответил, остаётся строкой «не ответил к моменту отправки»."""
    async with session_scope() as session:
        memo, _ = await load_visible(session, staff, memo_id, lock=True)
        require_author(memo, staff, MemoStatus.SIGNING)
        memo.status = MemoStatus.SUBMITTED
        memo.submitted_at = memo.updated_at = utc_now()
    logger.info("memo_submitted", memo_id=str(memo_id))


async def create_based_on(staff: StaffContext, memo_id: uuid.UUID) -> uuid.UUID:
    """«Создать новую на основе этой» после «На доработку» (§3.6): новый черновик с тем же
    текстом, адресатом и подписантами и ссылкой «взамен»; подписи собираются заново.
    Участников проверит отправка: за время доработки кто-то мог уйти из отеля."""
    async with session_scope() as session:
        source, rows = await load_visible(session, staff, memo_id)
        require_author(source, staff, MemoStatus.RETURNED)
        memo = Memo(
            id=uuid.uuid4(),
            tenant_id=source.tenant_id,
            kind=source.kind,
            template_key=source.template_key,
            author_id=staff.user_id,
            addressee_id=source.addressee_id,
            status=MemoStatus.DRAFT,
            based_on_id=source.id,
        )
        memo.title_ciphertext = seal_memo(memo, "title_ciphertext", memo_title(source))
        memo.body_ciphertext = seal_memo(memo, "body_ciphertext", memo_body(source))
        session.add(memo)
        await session.flush()
        _replace_signers(session, memo, tuple(signer_ids(rows)))
    logger.info("memo_draft_created", memo_id=str(memo.id), based_on_id=str(memo_id))
    return memo.id
