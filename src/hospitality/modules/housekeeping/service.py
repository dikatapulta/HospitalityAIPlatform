"""Бизнес-логика модуля housekeeping (spec 0046, ADR-021) — копия канона
`modules/requests/service.py`.

Единственный путь отметить выезд или сменить шаг уборки. Каждая функция — одна
транзакция: вызывается внутри `tenant_context`, открывает `session_scope()`;
события публикуются в той же транзакции, что бизнес-запись (P-6). Права (кто
какой шаг вправе нажать) режет кабинет (`staff_portal/turnovers.py`), домен
держит порядок шагов — тот же раздел ответственности, что у заявок.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.housekeeping.events import (
    RoomTurnoverReported,
    RoomTurnoverStatusChanged,
)
from hospitality.modules.housekeeping.models import RoomTurnover, TurnoverStatus
from hospitality.modules.housekeeping.schemas import (
    ActingUser,
    CleanedByStaff,
    RoomTurnoverRead,
    TurnoverReport,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND = "ERR-HOUSEKEEPING-001"
ERR_HOUSEKEEPING_INVALID_TRANSITION = "ERR-HOUSEKEEPING-002"

# Имя partial UNIQUE «один открытый путь на номер» (модель) — по нему гонка
# двух отметок выезда опознаётся в тексте IntegrityError.
_OPEN_ROOM_CONSTRAINT: Final = "uq_room_turnovers_tenant_room_open"

# Путь номера (spec 0046 §3): строго по порядку, отменить можно любой открытый.
TURNOVER_TRANSITIONS: Final[dict[TurnoverStatus, frozenset[TurnoverStatus]]] = {
    TurnoverStatus.REPORTED: frozenset({TurnoverStatus.INSPECTED, TurnoverStatus.CANCELLED}),
    TurnoverStatus.INSPECTED: frozenset({TurnoverStatus.CLEANING, TurnoverStatus.CANCELLED}),
    TurnoverStatus.CLEANING: frozenset({TurnoverStatus.CLEANED, TurnoverStatus.CANCELLED}),
    TurnoverStatus.CLEANED: frozenset({TurnoverStatus.READY, TurnoverStatus.CANCELLED}),
    TurnoverStatus.READY: frozenset(),
    TurnoverStatus.CANCELLED: frozenset(),
}

# Открытые статусы выводятся из карты, чтобы не разъехаться с ней при правках.
OPEN_TURNOVER_STATUSES: Final = frozenset(
    status for status, transitions in TURNOVER_TRANSITIONS.items() if transitions
)

# Повторная отметка выезда в этом окне от первой — дубль того же выезда (второй
# ресепшенист, двойное нажатие). Открытый путь старше — забытый путь прошлого
# проживания: следующий выезд того же номера раньше не случается (номер надо
# убрать, заселить и снова освободить). Spec 0046 §3.
DUPLICATE_REPORT_WINDOW: Final = timedelta(hours=6)


async def report_checkout(
    room_number: str, *, acting_user: ActingUser | None = None
) -> TurnoverReport:
    """Отметить выезд: новый путь номера в `reported` + `room_turnover.reported`.

    У номера уже есть открытый путь, отмеченный не раньше `DUPLICATE_REPORT_WINDOW`,
    — он и возвращается (`created=False`), без второй строки и без события:
    двойное нажатие и второй ресепшенист — не ошибка. Открытый путь старше окна
    закрывается отменой без «кто» (warning-лог), и выезд открывает новый путь:
    вчерашняя непринятая уборка не должна проглотить сегодняшний выезд. Гонку двух
    одновременных отметок решает partial UNIQUE, а не проверка: проигравший
    перечитывает путь победителя.
    """
    room = room_number.strip()
    try:
        async with session_scope() as session:
            now = utc_now()
            existing = await _find_open(session, room, for_update=True)
            if existing is not None:
                if existing.reported_at >= now - DUPLICATE_REPORT_WINDOW:
                    return TurnoverReport(
                        turnover=RoomTurnoverRead.model_validate(existing), created=False
                    )
                await _close_stale(session, existing, now)
            turnover = RoomTurnover(room_number=room, reported_at=now)
            if acting_user is not None:
                turnover.reported_by_user_id = acting_user.user_id
                turnover.reported_by_display_name = acting_user.display_name
            session.add(turnover)
            await session.flush()
            await publish(session, RoomTurnoverReported(turnover_id=turnover.id, room_number=room))
    except IntegrityError as error:
        if _OPEN_ROOM_CONSTRAINT not in str(error):
            raise
        async with session_scope() as session:
            winner = await _find_open(session, room)
        if winner is None:
            # Победитель гонки успел закрыть путь — редкость; честный отказ лучше
            # тихой второй попытки.
            raise
        return TurnoverReport(turnover=RoomTurnoverRead.model_validate(winner), created=False)
    logger.info("room_turnover_reported", turnover_id=str(turnover.id), room_number=room)
    return TurnoverReport(turnover=RoomTurnoverRead.model_validate(turnover), created=True)


async def change_turnover_status(
    turnover_id: uuid.UUID,
    new_status: TurnoverStatus,
    *,
    acting_user: ActingUser | None = None,
    inspection_note: str | None = None,
) -> RoomTurnoverRead:
    """Следующий шаг пути номера + `room_turnover.status_changed`.

    Недопустимый переход (не по порядку, из закрытого, в тот же) —
    ERR-HOUSEKEEPING-002 (409). Строка берётся `FOR UPDATE`: из двух нажатий
    «Проверено» проходит одно, второе получает 409 по свежему статусу.
    `inspection_note` пишется только на переходе в `inspected` (кнопка
    «Замечание»), на прочих игнорируется с warning-логом.
    """
    async with session_scope() as session:
        turnover = await _get_or_raise(session, turnover_id, for_update=True)
        old_status = turnover.status
        if new_status not in TURNOVER_TRANSITIONS[old_status]:
            raise AppError(
                code=ERR_HOUSEKEEPING_INVALID_TRANSITION,
                message=(
                    f"Cannot change room turnover from {old_status.value!r} to {new_status.value!r}"
                ),
                status_code=409,
            )
        turnover.status = new_status
        _stamp_step(turnover, new_status, utc_now(), acting_user)
        if new_status is TurnoverStatus.INSPECTED:
            turnover.inspection_note = (inspection_note or "").strip() or None
        elif inspection_note is not None:
            logger.warning(
                "inspection_note_ignored", turnover_id=str(turnover.id), new_status=new_status
            )
        await publish(
            session,
            RoomTurnoverStatusChanged(
                turnover_id=turnover.id,
                room_number=turnover.room_number,
                old_status=old_status,
                new_status=new_status,
            ),
        )
    logger.info(
        "room_turnover_status_changed",
        turnover_id=str(turnover.id),
        old_status=old_status.value,
        new_status=new_status.value,
    )
    return RoomTurnoverRead.model_validate(turnover)


def _stamp_step(
    turnover: RoomTurnover, status: TurnoverStatus, now: datetime, actor: ActingUser | None
) -> None:
    """«Когда» пишется всегда, «кто» — только с личностью (канон spec 0035 §3)."""
    user_id = actor.user_id if actor is not None else None
    name = actor.display_name if actor is not None else None
    if status is TurnoverStatus.INSPECTED:
        turnover.inspected_at = now
        turnover.inspected_by_user_id = user_id
        turnover.inspected_by_display_name = name
    elif status is TurnoverStatus.CLEANING:
        turnover.cleaning_started_at = now
        turnover.cleaning_started_by_user_id = user_id
        turnover.cleaning_started_by_display_name = name
    elif status is TurnoverStatus.CLEANED:
        turnover.cleaned_at = now
        turnover.cleaned_by_user_id = user_id
        turnover.cleaned_by_display_name = name
    elif status is TurnoverStatus.READY:
        turnover.ready_at = now
        turnover.accepted_by_user_id = user_id
        turnover.accepted_by_display_name = name
    elif status is TurnoverStatus.CANCELLED:
        turnover.cancelled_at = now
        turnover.cancelled_by_user_id = user_id
        turnover.cancelled_by_display_name = name


async def get_turnover(turnover_id: uuid.UUID) -> RoomTurnoverRead:
    """Путь номера по id; чужой тенант и несуществующий — ERR-HOUSEKEEPING-001."""
    async with session_scope() as session:
        return RoomTurnoverRead.model_validate(await _get_or_raise(session, turnover_id))


async def list_open_turnovers(*, limit: int) -> list[RoomTurnoverRead]:
    """Открытые пути, давние сверху (доска, spec 0046 §6); `limit` — страховка скана."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(RoomTurnover)
            .where(RoomTurnover.status.in_(OPEN_TURNOVER_STATUSES))
            .order_by(RoomTurnover.reported_at, RoomTurnover.id)
            .limit(limit)
        )
        return [RoomTurnoverRead.model_validate(row) for row in rows]


async def list_turnovers_ready_since(
    ready_after: datetime, *, limit: int
) -> list[RoomTurnoverRead]:
    """Принятые с `ready_after` (полночь отеля считает вызывающий), свежие сверху."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(RoomTurnover)
            .where(
                RoomTurnover.status == TurnoverStatus.READY, RoomTurnover.ready_at >= ready_after
            )
            .order_by(RoomTurnover.ready_at.desc(), RoomTurnover.id)
            .limit(limit)
        )
        return [RoomTurnoverRead.model_validate(row) for row in rows]


async def count_cleaned_since(cleaned_after: datetime) -> list[CleanedByStaff]:
    """«Сегодня убрано»: кто сколько раз нажал «Убрано» с `cleaned_after`.

    Отменённые пути не считаются — номер, отменённый после уборки, был ошибочным
    выездом. Порядок — больше сверху, при равенстве по имени. Без личности
    (шаг не из кабинета) строки в счёт не попадают.
    """
    count = func.count(RoomTurnover.id)
    async with session_scope() as session:
        rows = await session.execute(
            select(RoomTurnover.cleaned_by_display_name, count)
            .where(
                RoomTurnover.cleaned_at >= cleaned_after,
                RoomTurnover.status != TurnoverStatus.CANCELLED,
                RoomTurnover.cleaned_by_display_name.is_not(None),
            )
            .group_by(RoomTurnover.cleaned_by_user_id, RoomTurnover.cleaned_by_display_name)
            .order_by(count.desc(), RoomTurnover.cleaned_by_display_name)
        )
        return [CleanedByStaff(display_name=str(name), count=int(total)) for name, total in rows]


async def clear_expired_inspection_notes(*, created_before: datetime) -> int:
    """Обнулить замечания путей старше `created_before` (spec 0046 §8, ретеншн 0032).

    Замечание может пересказывать гостя — срок тот же, что у `resolution_note`
    заявки. Статусы, времена и имена остаются: это отчёт «кто сколько сделал».
    Идемпотентна (P-8): повторный прогон обновляет 0 строк.
    """
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(RoomTurnover)
                .where(
                    RoomTurnover.created_at < created_before,
                    RoomTurnover.inspection_note.is_not(None),
                )
                .values(inspection_note=None)
            ),
        )
    return result.rowcount or 0


async def _close_stale(session: AsyncSession, turnover: RoomTurnover, now: datetime) -> None:
    """Забытый путь прошлого проживания → `cancelled` без «кто» + событие (spec 0046 §3).

    `flush` до вставки нового пути: partial UNIQUE проверяется на каждом
    операторе, и номер должен освободиться раньше INSERT.
    """
    old_status = turnover.status
    turnover.status = TurnoverStatus.CANCELLED
    _stamp_step(turnover, TurnoverStatus.CANCELLED, now, None)
    await publish(
        session,
        RoomTurnoverStatusChanged(
            turnover_id=turnover.id,
            room_number=turnover.room_number,
            old_status=old_status,
            new_status=TurnoverStatus.CANCELLED,
        ),
    )
    await session.flush()
    logger.warning(
        "room_turnover_stale_closed",
        turnover_id=str(turnover.id),
        room_number=turnover.room_number,
        old_status=old_status.value,
        reported_at=turnover.reported_at.isoformat(),
    )


async def _find_open(
    session: AsyncSession, room_number: str, *, for_update: bool = False
) -> RoomTurnover | None:
    """`for_update` — отметка выезда: забытый путь закрывается под блокировкой
    строки, чтобы не перетереть одновременное «Принять» (канон
    `change_turnover_status`)."""
    statement = select(RoomTurnover).where(
        RoomTurnover.room_number == room_number,
        RoomTurnover.status.in_(OPEN_TURNOVER_STATUSES),
    )
    if for_update:
        statement = statement.with_for_update()
    return cast("RoomTurnover | None", await session.scalar(statement))


async def _get_or_raise(
    session: AsyncSession, turnover_id: uuid.UUID, *, for_update: bool = False
) -> RoomTurnover:
    statement = select(RoomTurnover).where(RoomTurnover.id == turnover_id)
    if for_update:
        statement = statement.with_for_update()
    turnover = await session.scalar(statement)
    if turnover is None:
        raise AppError(
            code=ERR_HOUSEKEEPING_TURNOVER_NOT_FOUND,
            message="Room turnover not found",
            status_code=404,
        )
    return turnover
