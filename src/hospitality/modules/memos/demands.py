"""Требование письменного объяснения (spec 0077 §6): фото бумажного, получение, срок.

Требование в системе — не документ, а способ вручить бумажный и зафиксировать
получение. Руководитель (`manager` — у любого, `senior` — у своих служб; права —
`access.check_demand_target`) выбирает сотрудника и прикрепляет фото подписанного
требования — канон `platform/files.py`, назначение `memo_demand`: проверка в песочнице,
WebP без метаданных, шифрование; модуль держит только id файла. Сотрудник жмёт
«Получил(а)» с подтверждением (`staff_confirm`, как подпись в листе): это время
получения, от него — срок (`deadline.py`). Доставка пуша и открытие страницы
получением не считаются. В логах — id и способ подтверждения, без имён (§8).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, or_, select

from hospitality.modules.memos.access import (
    can_see,
    check_demand_target,
    load_demand,
    no_signature_sample,
    not_found,
    wrong_state,
)
from hospitality.modules.memos.codec import demand_subject, receipt, seal_receipt
from hospitality.modules.memos.deadline import explanation_due
from hospitality.modules.memos.models import Memo, MemoDemand
from hospitality.modules.memos.schemas import (
    DemandCard,
    DemandExplanation,
    DemandState,
    DemandSummary,
    MemoBox,
    PasskeyAnswer,
    PasswordAnswer,
)
from hospitality.modules.memos.service import hotel_config
from hospitality.modules.memos.signing import confirm_answer, current_sample
from hospitality.platform.files import (
    ERR_FILE_NOT_FOUND,
    FilePurpose,
    StoredFile,
    delete_file,
    read_file,
    store_upload,
)
from hospitality.platform.staff_confirm import (
    ConfirmationRequest,
    ProofStatus,
    begin_confirmation,
    verify_proof,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)


async def create_demand(
    staff: StaffContext, employee_id: uuid.UUID, data: bytes, original_name: str
) -> uuid.UUID:
    """«Затребовать объяснение»: фото подписанного требования обязательно (§6.1).

    Права проверяются до песочницы: отказ не тратит проверку файла и не оставляет
    его на томе. Негодный файл — ERR-FILES-001…003; упала запись строки — файл
    удаляется тут же (канон `room_service/photos.py`)."""
    await check_demand_target(staff, employee_id)
    meta = await store_upload(
        FilePurpose.MEMO_DEMAND, data, original_name, uploaded_by_user_id=staff.user_id
    )
    demand_id = uuid.uuid4()
    try:
        async with session_scope() as session:
            session.add(
                MemoDemand(
                    id=demand_id,
                    tenant_id=staff.tenant_id,
                    demanded_by_id=staff.user_id,
                    employee_id=employee_id,
                    file_id=meta.id,
                )
            )
    except BaseException:
        await delete_photo_quietly(meta.id)
        raise
    logger.info("memo_demand_created", demand_id=str(demand_id))
    return demand_id


def _require_receiver(demand: MemoDemand, staff: StaffContext) -> None:
    # «Получил(а)» — только тому, у кого требуют; затребовавшему кнопки нет.
    if demand.employee_id != staff.user_id:
        raise not_found()
    if demand.received_at is not None:
        raise wrong_state()


async def begin_receipt(staff: StaffContext, demand_id: uuid.UUID) -> ConfirmationRequest:
    """Окно подтверждения «Получил(а)»; без образца подписи — ERR-MEMO-003."""
    async with session_scope() as session:
        demand = await load_demand(session, staff, demand_id)
        _require_receiver(demand, staff)
        if await current_sample(session, staff.user_id) is None:
            raise no_signature_sample()
        confirmed = demand_subject(demand)
    return await begin_confirmation(staff, confirmed)


async def confirm_receipt(
    staff: StaffContext, demand_id: uuid.UUID, answer: PasskeyAnswer | PasswordAnswer
) -> datetime:
    """Принять подтверждение получения; вернуть срок объяснения (§6.3).

    Время получения — время подтверждения. Повтор из второй вкладки — ERR-MEMO-002,
    подтверждение при этом израсходовано."""
    async with session_scope() as session:
        demand = await load_demand(session, staff, demand_id)
        _require_receiver(demand, staff)
        confirmed = demand_subject(demand)
    proof = await confirm_answer(staff, confirmed, answer)
    config = await hotel_config(staff.tenant_id)
    async with session_scope() as session:
        demand = await load_demand(session, staff, demand_id, lock=True)
        _require_receiver(demand, staff)
        due_at = explanation_due(proof.confirmed_at, config.tzinfo, config.holidays)
        demand.received_at = proof.confirmed_at
        demand.received_proof_ciphertext = seal_receipt(demand, proof)
        demand.due_at = due_at
    logger.info("memo_demand_received", demand_id=str(demand_id), method=proof.method.value)
    return due_at


def _state(demand: MemoDemand, sent: Collection[datetime], now: datetime) -> DemandState:
    if demand.received_at is None:
        return DemandState.AWAITING_RECEIPT
    if sent:
        return DemandState.EXPLAINED
    if demand.due_at is not None and now > demand.due_at:
        return DemandState.EXPIRED
    return DemandState.OPEN


def _summary_fields(demand: MemoDemand, sent: Collection[datetime]) -> dict[str, object]:
    return {
        "id": demand.id,
        "demanded_by_id": demand.demanded_by_id,
        "employee_id": demand.employee_id,
        "created_at": demand.created_at,
        "received_at": demand.received_at,
        "due_at": demand.due_at,
        "state": _state(demand, sent, utc_now()),
    }


def _late(demand: MemoDemand, memo: Memo) -> bool:
    return demand.due_at is not None and memo.sent_at is not None and memo.sent_at > demand.due_at


async def get_demand(staff: StaffContext, demand_id: uuid.UUID) -> DemandCard:
    """Карточка требования затребовавшему и сотруднику; иначе ERR-MEMO-001.

    Объяснительные по нему — те, что видит вошедший: черновик — только автор."""
    async with session_scope() as session:
        demand = await load_demand(session, staff, demand_id)
        memos = list(
            await session.scalars(
                select(Memo).where(Memo.demand_id == demand.id).order_by(Memo.created_at)
            )
        )
    stored = receipt(demand)
    status: ProofStatus | None = None
    if stored is not None:
        status = await verify_proof(
            demand_subject(demand), stored, signer_user_id=demand.employee_id
        )
    sent = [memo.sent_at for memo in memos if memo.sent_at is not None]
    return DemandCard.model_validate(
        {
            **_summary_fields(demand, sent),
            "received_method": stored.method if stored else None,
            "received_proof_status": status,
            "explanations": [
                DemandExplanation(
                    id=memo.id,
                    number=memo.number,
                    status=memo.status,
                    sent_at=memo.sent_at,
                    late=_late(demand, memo),
                )
                for memo in memos
                if can_see(memo, (), staff.user_id)
            ],
        }
    )


def _box_filter(box: MemoBox, user_id: uuid.UUID) -> ColumnElement[bool]:
    if box is MemoBox.WAITING:  # «Ждут меня»: получить требование (§5)
        return (MemoDemand.employee_id == user_id) & MemoDemand.received_at.is_(None)
    if box is MemoBox.MINE:  # «Мои»: я затребовал
        return MemoDemand.demanded_by_id == user_id
    return or_(MemoDemand.demanded_by_id == user_id, MemoDemand.employee_id == user_id)


async def list_demands(staff: StaffContext, box: MemoBox) -> list[DemandSummary]:
    """Требования вкладки реестра — свежие сверху."""
    async with session_scope() as session:
        demands = list(
            await session.scalars(
                select(MemoDemand)
                .where(_box_filter(box, staff.user_id))
                .order_by(MemoDemand.created_at.desc())
            )
        )
        sent: dict[uuid.UUID, list[datetime]] = {demand.id: [] for demand in demands}
        if demands:
            rows = await session.execute(
                select(Memo.demand_id, Memo.sent_at).where(
                    Memo.demand_id.in_(sent), Memo.sent_at.is_not(None)
                )
            )
            for memo_demand_id, sent_at in rows:
                if memo_demand_id is not None and sent_at is not None:
                    sent[memo_demand_id].append(sent_at)
    return [
        DemandSummary.model_validate(_summary_fields(demand, sent[demand.id])) for demand in demands
    ]


async def read_demand_photo(staff: StaffContext, demand_id: uuid.UUID) -> StoredFile:
    """Фото требования — затребовавшему и сотруднику; иначе ERR-MEMO-001. Сбои
    хранилища — коды узла (ERR-FILES-005, -006)."""
    async with session_scope() as session:
        demand = await load_demand(session, staff, demand_id)
    return await read_file(demand.file_id)


async def delete_photo_quietly(file_id: uuid.UUID) -> bool:
    """Удалить фото требования; вернуть, удалено ли (или его уже нет).

    Не удалилось (хранилище недоступно) — WARNING, а не исключение поверх исходного
    (канон `documents.delete_files_quietly`): ретеншн повторит в следующий проход."""
    try:
        await delete_file(file_id)
    except AppError as exc:
        if exc.code == ERR_FILE_NOT_FOUND:
            return True
        logger.warning(
            "memo_demand_photo_cleanup_failed", file_id=str(file_id), error_code=exc.code
        )
        return False
    except Exception:
        logger.warning("memo_demand_photo_cleanup_failed", file_id=str(file_id), exc_info=True)
        return False
    return True
