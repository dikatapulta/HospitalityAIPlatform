"""Шаги плана сверки PMS — каждый своей транзакцией (spec 0058 §2.2, ADR-004 п. 1–3).

Путь — тот же, что у кабинета (P-12): заселение — `service.open_stay`, выезд —
`service.close_stay`, события `stay.*` и `FOR UPDATE` — те же. Отличие одно:
журнал пишет `record_automatic_action` («Сотрудник — автоматически», канон
spec 0043 §2.3), личности нет. Строка берётся `FOR UPDATE` без `SKIP LOCKED` и
с перепроверкой условий плана в той же выборке: сотрудник, держащий Stay, своё
действие заканчивает, сверка ждёт и видит итог — Stay закрыт или переселён
руками, шаг ничего не делает (False). Комнату, которую сотрудник занял между
планом и шагом, отвергает partial unique — тоже False: следующий снимок
разложит её заново.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests.models import Stay, StaySource, StayStatus
from hospitality.modules.guests.pms_plan import Adopt, CheckIn, CheckOut, Extend, Move
from hospitality.modules.guests.schemas import MAX_GUESTS_PER_STAY, StayCheckIn
from hospitality.modules.guests.service import close_stay, new_access_code_hash, open_stay
from hospitality.platform.staff_actions import (
    StaffAction,
    StaffActionRecord,
    record_automatic_action,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Индексы, которыми БД отвергает гонку с кабинетом: комната или бронь уже у
# другого активного Stay (models.Stay).
_LOST_RACE_CONSTRAINTS: Final = (
    "uq_stays_tenant_room_checked_in",
    "uq_stays_tenant_pms_booking_checked_in",
)


async def apply_check_out(step: CheckOut) -> bool:
    """Выезд из Opera (п. 3): код, ссылки и сессии гаснут, `stay.checked_out`."""
    async with session_scope() as session:
        stay = await _lock_pms_stay(session, step.stay_id, step.booking_ref)
        if stay is None:
            return False
        await close_stay(session, stay, utc_now())
        _journal(session, StaffAction.STAY_PMS_CHECKED_OUT, stay)
    logger.info("pms_stay_checked_out", stay_id=str(stay.id), room_number=stay.room_number)
    return True


async def apply_move(step: Move) -> bool:
    """Переселение из Opera (п. 2): доступ гостя следует за Stay сам."""
    try:
        async with session_scope() as session:
            stay = await _lock_pms_stay(session, step.stay_id, step.booking_ref)
            if stay is None or stay.room_number != step.from_room:
                return False
            stay.room_number = step.to_room
            await session.flush()
            _journal(session, StaffAction.STAY_PMS_MOVED, stay, room_number=step.from_room)
    except IntegrityError as error:
        return _lost_race(error, "move")
    logger.info(
        "pms_stay_moved", stay_id=str(step.stay_id), old_room=step.from_room, new_room=step.to_room
    )
    return True


async def apply_extend(step: Extend) -> bool:
    """Срок из Opera (п. 2): продление и ранний выезд — одно поле."""
    if step.check_out_at <= utc_now():
        return False  # срок прошёл, пока шёл прогон: гостя закроет выезд (п. 3)
    async with session_scope() as session:
        stay = await _lock_pms_stay(session, step.stay_id, step.booking_ref)
        if stay is None:
            return False
        stay.check_out_at = step.check_out_at
        _journal(session, StaffAction.STAY_PMS_EXTENDED, stay)
    logger.info(
        "pms_stay_extended",
        stay_id=str(stay.id),
        room_number=stay.room_number,
        check_out_at=step.check_out_at.isoformat(),
    )
    return True


async def apply_check_in(step: CheckIn) -> bool:
    """Заселение из Opera (п. 1): Stay и код, plaintext кода не видит никто."""
    data = StayCheckIn(
        room_number=step.row.room_number,
        check_out_at=step.check_out_at,
        guests_count=_guests(step.row.guests_count or 1),
    )
    code_hash = await new_access_code_hash()
    try:
        async with session_scope() as session:
            stay = await open_stay(
                session,
                data,
                code_hash=code_hash,
                source=StaySource.PMS,
                booking_ref=step.row.booking_ref,
            )
            _journal(session, StaffAction.STAY_PMS_CHECKED_IN, stay)
    except IntegrityError as error:
        return _lost_race(error, "check_in")
    logger.info("pms_stay_checked_in", stay_id=str(stay.id), room_number=stay.room_number)
    return True


async def apply_adopt(step: Adopt) -> bool:
    """Ручной Stay этой брони становится PMS-Stay: код, QR и сессии гостя живут."""
    try:
        async with session_scope() as session:
            stay = await session.scalar(
                select(Stay)
                .where(
                    Stay.id == step.stay_id,
                    Stay.status == StayStatus.CHECKED_IN,
                    Stay.source == StaySource.MANUAL,
                    Stay.room_number == step.room_number,
                )
                .with_for_update()
            )
            if stay is None:
                return False
            stay.source = StaySource.PMS
            stay.pms_booking_ref = step.row.booking_ref
            stay.pms_missed_snapshots = 0
            if step.check_out_at is not None:
                stay.check_out_at = step.check_out_at
            if step.row.guests_count is not None:
                stay.guests_count = _guests(step.row.guests_count)
            await session.flush()
            _journal(session, StaffAction.STAY_PMS_LINKED, stay)
    except IntegrityError as error:
        return _lost_race(error, "adopt")
    logger.info("pms_stay_linked", stay_id=str(step.stay_id), room_number=step.room_number)
    return True


async def _lock_pms_stay(
    session: AsyncSession, stay_id: uuid.UUID, booking_ref: str
) -> Stay | None:
    """Активный PMS-Stay этой брони под `FOR UPDATE`; None — его уже закрыли или отвязали."""
    stay: Stay | None = await session.scalar(
        select(Stay)
        .where(
            Stay.id == stay_id,
            Stay.status == StayStatus.CHECKED_IN,
            Stay.source == StaySource.PMS,
            Stay.pms_booking_ref == booking_ref,
        )
        .with_for_update()
    )
    return stay


def _journal(
    session: AsyncSession, action: StaffAction, stay: Stay, *, room_number: str | None = None
) -> None:
    """Строка журнала «автоматически»: комната, у переселения — откуда и куда (§2.5)."""
    record_automatic_action(
        session,
        StaffActionRecord(
            action=action,
            stay_id=stay.id,
            room_number=room_number or stay.room_number,
            room_number_to=stay.room_number if room_number else None,
        ),
    )


def _guests(count: int) -> int:
    """Число гостей под потолок домена: предел файла проверяет адаптер (§2.4), Fake — нет."""
    return min(count, MAX_GUESTS_PER_STAY)


def _lost_race(error: IntegrityError, step: str) -> bool:
    """Комнату или бронь занял кабинет между планом и шагом — шаг пропущен (False)."""
    if not any(name in str(error) for name in _LOST_RACE_CONSTRAINTS):
        raise error
    logger.warning("pms_sync_step_lost_race", step=step)
    return False
