"""Объяснительная (spec 0077 §6): черновик, правка, «Дополнить».

Тот же `Memo`, вид `explanation`: подписантов и решения нет, ИИ-оформления нет (§7).
По требованию — только тот, у кого требуют, и только после «Получил(а)»; адресат —
затребовавший. Без требования — автор сам выбирает адресата (`senior` или `manager`).
«Подать» — `SendIntent` через `signing.begin_action` / `complete_action`: номер
«ОБ-2026/3», подтверждение, сразу `submitted`. Поданное не меняется; «Дополнить» —
новый черновик со ссылкой `based_on_id` на поданную, тем же адресатом и требованием.
Подать можно и после срока — с фактическим временем (§6.4).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hospitality.modules.memos.access import (
    check_participants,
    invalid_input,
    load_demand,
    load_visible,
    not_found,
    require_author,
    wrong_state,
)
from hospitality.modules.memos.codec import seal_memo
from hospitality.modules.memos.models import Memo, MemoKind, MemoStatus
from hospitality.modules.memos.schemas import ExplanationDraftInput, ExplanationText
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)


async def _new_draft(
    staff: StaffContext,
    text: ExplanationDraftInput | ExplanationText,
    addressee_id: uuid.UUID,
    *,
    demand_id: uuid.UUID | None,
    based_on_id: uuid.UUID | None,
) -> uuid.UUID:
    async with session_scope() as session:
        memo = Memo(
            id=uuid.uuid4(),
            tenant_id=staff.tenant_id,
            kind=MemoKind.EXPLANATION,
            author_id=staff.user_id,
            addressee_id=addressee_id,
            status=MemoStatus.DRAFT,
            demand_id=demand_id,
            based_on_id=based_on_id,
        )
        memo.title_ciphertext = seal_memo(memo, "title_ciphertext", text.title)
        memo.body_ciphertext = seal_memo(memo, "body_ciphertext", text.body)
        session.add(memo)
    logger.info(
        "memo_explanation_draft_created",
        memo_id=str(memo.id),
        by_demand=demand_id is not None,
        supplement=based_on_id is not None,
    )
    return memo.id


async def create_explanation(staff: StaffContext, draft: ExplanationDraftInput) -> uuid.UUID:
    """Новый черновик объяснительной: по требованию или адресату по своей инициативе.

    Чужое требование — ERR-MEMO-001; ещё не получено — ERR-MEMO-002: срок и право
    писать начинаются с «Получил(а)». Адресат без требования — ERR-PLATFORM-002 (422)."""
    if draft.demand_id is None:
        assert draft.addressee_id is not None  # схема требует ровно одно
        await check_participants(staff, draft.addressee_id, ())
        return await _new_draft(staff, draft, draft.addressee_id, demand_id=None, based_on_id=None)
    async with session_scope() as session:
        demand = await load_demand(session, staff, draft.demand_id)
    if demand.employee_id != staff.user_id:
        raise not_found()
    if demand.received_at is None:
        raise wrong_state()
    return await _new_draft(
        staff, draft, demand.demanded_by_id, demand_id=demand.id, based_on_id=None
    )


async def update_explanation(
    staff: StaffContext, memo_id: uuid.UUID, text: ExplanationText
) -> None:
    """Правка своего черновика объяснительной. Адресата меняет только черновик без
    требования и не дополнение — иначе ERR-PLATFORM-002 (`addressee_fixed`)."""
    if text.addressee_id is not None:
        await check_participants(staff, text.addressee_id, ())
    async with session_scope() as session:
        memo, _ = await load_visible(session, staff, memo_id, lock=True)
        require_author(memo, staff, MemoStatus.DRAFT)
        if memo.kind is not MemoKind.EXPLANATION:  # записка — `service.update_draft`
            raise not_found()
        if text.addressee_id is not None and text.addressee_id != memo.addressee_id:
            if memo.demand_id is not None or memo.based_on_id is not None:
                raise invalid_input("addressee_id", "addressee_fixed")
            memo.addressee_id = text.addressee_id
        memo.title_ciphertext = seal_memo(memo, "title_ciphertext", text.title)
        memo.body_ciphertext = seal_memo(memo, "body_ciphertext", text.body)
        memo.updated_at = utc_now()
    logger.info("memo_draft_updated", memo_id=str(memo_id), kind=MemoKind.EXPLANATION.value)


async def create_supplement(
    staff: StaffContext, explanation_id: uuid.UUID, text: ExplanationText
) -> uuid.UUID:
    """«Дополнить» поданную объяснительную (§6.4): новый черновик со ссылкой на неё,
    тем же адресатом и требованием. Не поданная — ERR-MEMO-002; не своя — ERR-MEMO-001."""
    async with session_scope() as session:
        source, _ = await load_visible(session, staff, explanation_id)
        require_author(source, staff, MemoStatus.SUBMITTED)
        if source.kind is not MemoKind.EXPLANATION:
            raise not_found()
    if text.addressee_id is not None and text.addressee_id != source.addressee_id:
        raise invalid_input("addressee_id", "addressee_fixed")
    return await _new_draft(
        staff, text, source.addressee_id, demand_id=source.demand_id, based_on_id=source.id
    )
