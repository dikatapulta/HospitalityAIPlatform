"""Обмен комнатами двух живущих гостей (#237, аудит 05.08 Г-6; spec 0033 §6).

«Рокировка» ресепшена — гость 305 переезжает в 410, гость 410 в 305. Двумя
`move_stay` её не сделать: первый же ход упирается в partial unique «один
активный Stay на комнату» (ERR-GUESTS-002), а выселение одного из гостей гасит
его код, QR и чат. Здесь обе строки берутся под блокировку и меняются комнатами
одной транзакцией через временный номер со «/»: у настоящей комнаты такого не
бывает (`ROOM_NUMBER_PATTERN`, #359), а снаружи транзакции его никто не видит.
Доступ обоих гостей следует за Stay сам, как при переселении (ADR-008 §3).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests.models import Stay, StayStatus
from hospitality.modules.guests.schemas import StayRead
from hospitality.modules.guests.service import (
    ERR_GUESTS_STAY_NOT_FOUND,
    _record_stay_action,
)
from hospitality.platform.staff_actions import StaffAction, StaffActor
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def swap_stay_rooms(
    stay_id: uuid.UUID, other_room_number: str, *, acting_user: StaffActor | None = None
) -> StayRead:
    """Stay `stay_id` и активный Stay комнаты `other_room_number` меняются комнатами.

    Код, QR и сессии обоих живут — комната читается из Stay на каждом действии.
    Журнал (с личностью) — две строки «Переселение», по одной на гостя. Любого
    из двух нет среди активных (выехал, пока сотрудник подтверждал) —
    ERR-GUESTS-001, ничего не меняется. Своя же комната — без изменений.
    """
    async with session_scope() as session:
        stay, other = await _lock_pair(session, stay_id, other_room_number)
        if stay.id == other.id:
            return StayRead.model_validate(stay)
        own_room, other_room = stay.room_number, other.room_number
        # Порядок записей — суть приёма: индекс проверяется на каждой строке
        # сразу, поэтому одна комната сперва освобождается временным номером.
        stay.room_number = f"/{stay.id.hex[:19]}"
        await session.flush()
        other.room_number = own_room
        await session.flush()
        stay.room_number = other_room
        await session.flush()
        for moved, room_from, room_to in (
            (stay, own_room, other_room),
            (other, other_room, own_room),
        ):
            _record_stay_action(
                session,
                acting_user,
                moved,
                StaffAction.STAY_MOVED,
                room_number=room_from,
                room_number_to=room_to,
            )
    logger.info(
        "stay_rooms_swapped",
        stay_id=str(stay.id),
        other_stay_id=str(other.id),
        room_number=stay.room_number,
        other_room_number=other.room_number,
    )
    return StayRead.model_validate(stay)


async def _lock_pair(
    session: AsyncSession, stay_id: uuid.UUID, other_room_number: str
) -> tuple[Stay, Stay]:
    """Обе строки под `FOR UPDATE` одним запросом в порядке id.

    Порядок — против взаимной блокировки двух встречных обменов одной пары
    (с карточки 305 и с карточки 410 разом): оба ждут одну и ту же первую
    строку. Блокировка — и против гонки с выездом и автовыездом (spec 0043
    §2.2), как у `move_stay`; после ожидания условия перепроверяются.
    """
    other_id = await session.scalar(
        select(Stay.id).where(
            Stay.room_number == other_room_number, Stay.status == StayStatus.CHECKED_IN
        )
    )
    rows = (
        await session.scalars(
            select(Stay)
            .where(
                Stay.id.in_({stay_id, other_id or stay_id}), Stay.status == StayStatus.CHECKED_IN
            )
            .order_by(Stay.id)
            .with_for_update()
        )
    ).all()
    by_id = {row.id: row for row in rows}
    stay = by_id.get(stay_id)
    other = by_id.get(other_id) if other_id is not None else None
    if stay is None or other is None or other.room_number != other_room_number:
        raise AppError(
            code=ERR_GUESTS_STAY_NOT_FOUND,
            message=f"Stay {stay_id} or an active stay in room {other_room_number!r} is gone",
            status_code=404,
        )
    return stay, other
