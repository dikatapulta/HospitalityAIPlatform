"""Бронь после «Отправить запрос» (spec 0064 §2.2, §2.4): чтение, стадия, отмена гостем, ретеншн.

Запрос — `bookings.py`, ответ ресторана — `answers.py`; здесь всё, что делается с
уже записанной бронью. Стадия выводится (как Р-4 spec 0038): из ответа, отметки
отмены, статуса заявки и часа брони — фоновой задачи нет. Бронь без заявки
(`request_created` ложно) не видит никто, кроме ретеншна. Каждая функция
вызывается внутри `tenant_context`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any, Final, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking.models import BookingAnswer, TableBooking
from hospitality.modules.table_booking.schemas import BookingRead, BookingStage
from hospitality.modules.table_booking.slots import load_config, local_clock, ru_day
from hospitality.platform.config import LocalizedText
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_table_booking

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_TABLEBOOKING_NOT_FOUND: Final = "ERR-TABLEBOOKING-004"
ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE: Final = "ERR-TABLEBOOKING-006"

# Бронь без заявки старше этого удаляет ретеншн (Р-3 spec 0038).
ABANDONED_AFTER: Final = timedelta(days=1)
# «Ваши брони» на странице — последние запросы проживания (лимит — 10 в сутки).
_STAY_LIST_LIMIT: Final = 20
_OPEN_STATUSES: Final = frozenset(
    {requests_api.RequestStatus.NEW, requests_api.RequestStatus.IN_PROGRESS}
)


async def get_booking_for_stay(booking_id: uuid.UUID, stay_id: uuid.UUID) -> BookingRead:
    """Бронь этого проживания; чужая, без заявки или несуществующая — 004."""
    async with session_scope() as session:
        row = await session.scalar(
            select(TableBooking).where(
                TableBooking.id == booking_id,
                TableBooking.stay_id == stay_id,
                TableBooking.request_created,
            )
        )
    if row is None:
        raise not_found(f"Table booking {booking_id} not found")
    return (await read_bookings([row]))[0]


async def list_bookings_for_stay(stay_id: uuid.UUID) -> list[BookingRead]:
    """«Ваши брони» (§2.2): запросы проживания, новые первыми."""
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(TableBooking)
                .where(TableBooking.stay_id == stay_id, TableBooking.request_created)
                .order_by(TableBooking.created_at.desc(), TableBooking.id)
                .limit(_STAY_LIST_LIMIT)
            )
        ).all()
    return await read_bookings(rows)


async def get_booking_by_request(request_id: uuid.UUID) -> BookingRead | None:
    """Бронь заявки — подписчику сообщений гостю; None — заявка не бронь.

    Заявка «Гость отменил бронь» бронью не считается: о ней гостю не пишут.
    """
    async with session_scope() as session:
        row = await session.scalar(
            select(TableBooking).where(TableBooking.request_id == request_id)
        )
    if row is None:
        return None
    return (await read_bookings([row]))[0]


async def booking_guest_identity(request_id: uuid.UUID) -> uuid.UUID | None:
    """Идентичность гостя брони — адрес сообщений в его веб-чат; None — нет."""
    async with session_scope() as session:
        identity: uuid.UUID | None = await session.scalar(
            select(TableBooking.guest_identity_id).where(TableBooking.request_id == request_id)
        )
    return identity


async def cancel_by_guest(booking_id: uuid.UUID, stay_id: uuid.UUID) -> BookingRead:
    """Гость отменяет запрос до ответа или бронь после «Подтвердить» — до часа брони (§2.4).

    Отметка пишется под `FOR UPDATE` строки брони: ответ ресторана берёт ту же
    блокировку, и из «гость отменяет» и «ресторан отвечает» выигрывает один.
    До ответа — заявка `cancelled` (гость видит отмену на странице, в чат не
    пишется); проиграл переход — отметка снимается, 006. После «Подтвердить» —
    новая заявка «Гость отменил бронь» в службе ресторана, её id пишется вместе с
    отметкой (Р-3, ревью PR #711 Н-4). Час прошёл, уже отменена или ответ не
    «Подтвердить» — 006.
    """
    now = utc_now()
    async with session_scope() as session:
        row = await session.scalar(
            select(TableBooking)
            .where(
                TableBooking.id == booking_id,
                TableBooking.stay_id == stay_id,
                TableBooking.request_created,
            )
            .with_for_update()
        )
        if row is None:
            raise not_found(f"Table booking {booking_id} not found")
        if row.cancelled_by_guest_at is not None or row.booking_at <= now:
            raise cancel_not_applicable(booking_id)
        if row.answer not in (None, BookingAnswer.CONFIRMED):
            raise cancel_not_applicable(booking_id)
        row.cancelled_by_guest_at = now
        confirmed = row.answer is BookingAnswer.CONFIRMED
        if confirmed:
            row.cancel_request_id = uuid.uuid4()
    if confirmed:
        await ensure_cancel_request(row)
    else:
        await _cancel_request_of(row, now)
    record_table_booking("cancelled_by_guest")
    logger.info(
        "table_booking_cancelled_by_guest", booking_id=str(booking_id), after_confirm=confirmed
    )
    return await get_booking_for_stay(booking_id, stay_id)


async def _cancel_request_of(row: TableBooking, marked_at: datetime) -> None:
    """Отмена заявки запроса без ответа; проиграла переход — отметка снимается (006)."""
    try:
        await requests_api.change_request_status(
            row.request_id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.GUEST,
        )
    except AppError as error:
        async with session_scope() as session:
            await session.execute(
                update(TableBooking)
                .where(TableBooking.id == row.id, TableBooking.cancelled_by_guest_at == marked_at)
                .values(cancelled_by_guest_at=None)
            )
        if error.code == requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION:
            raise cancel_not_applicable(row.id) from error
        raise


async def ensure_cancel_request(row: TableBooking) -> None:
    """Заявка «Гость отменил бронь» (идемпотентно по `cancel_request_id`) и отметка «есть».

    Зовут отмена и показ брони гостю: сбой между отметкой и заявкой доводит
    следующий показ страницы (Н-4). Служба — та, где сейчас заявка брони: она и
    отвечала (передали — новая служба).
    """
    if row.cancel_request_id is None or row.cancel_request_created:
        return
    config = await load_config()
    original = await requests_api.get_request(row.request_id)
    await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=original.category_id,
            origin=requests_api.ServiceRequestOrigin.GUEST_FORM,
            summary=_cancel_summary(row, original.room_number, config.tzinfo),
            room_number=original.room_number,
            guest_language=row.guest_language,
        ),
        request_id=row.cancel_request_id,
        stay_id=row.stay_id,
    )
    async with session_scope() as session:
        await session.execute(
            update(TableBooking)
            .where(TableBooking.id == row.id)
            .values(cancel_request_created=True)
        )
    row.cancel_request_created = True
    logger.info(
        "table_booking_cancel_request_created",
        booking_id=str(row.id),
        request_id=str(row.cancel_request_id),
    )


def _cancel_summary(row: TableBooking, room_number: str | None, zone: tzinfo) -> str:
    """Суть заявки отмены для службы ресторана (§5), по-русски."""
    name = LocalizedText.model_validate(row.restaurant_names).text_for("ru")
    local = row.booking_at.astimezone(zone)
    room = f", № {room_number}" if room_number else ""
    return (
        f"Гость отменил бронь: {name}, {ru_day(local.date())}, {local:%H:%M}, "
        f"{row.party_size} гост.{room} — уберите из книги броней"
    )


async def read_bookings(rows: Sequence[TableBooking]) -> list[BookingRead]:
    """Брони с заявками и стадией; недоведённая заявка отмены доводится (Н-4)."""
    if not rows:
        return []
    for row in rows:
        await ensure_cancel_request(row)
    zone = (await load_config()).tzinfo
    requests = {
        request.id: request
        for request in await requests_api.list_requests_by_ids([row.request_id for row in rows])
    }
    now = utc_now()
    return [
        read_booking(row, requests[row.request_id], zone, now)
        for row in rows
        if row.request_id in requests
    ]


def read_booking(
    row: TableBooking,
    request: requests_api.ServiceRequestRead,
    zone: tzinfo,
    now: datetime,
) -> BookingRead:
    stage = booking_stage(row, request.status, now)
    local = row.booking_at.astimezone(zone)
    cancellable = row.booking_at > now and (
        stage is BookingStage.WAITING or stage is BookingStage.CONFIRMED
    )
    return BookingRead(
        id=row.id,
        request_id=row.request_id,
        number=request.daily_number,
        restaurant_key=row.restaurant_key,
        restaurant_names=LocalizedText.model_validate(row.restaurant_names),
        booking_at=row.booking_at,
        local_date=local.date(),
        local_time=local.strftime("%H:%M"),
        party_size=row.party_size,
        guest_language=row.guest_language,
        room_number=request.room_number,
        stage=stage,
        answer=row.answer,
        proposed_at=row.proposed_at,
        proposed_time=local_clock(row.proposed_at, zone) if row.proposed_at else None,
        answered_at=row.answered_at,
        cancelled_by_guest_at=row.cancelled_by_guest_at,
        cancellable=cancellable,
        created_at=row.created_at,
    )


def booking_stage(
    row: TableBooking, status: requests_api.RequestStatus, now: datetime
) -> BookingStage:
    """Стадия брони (§2.2): отмена гостем главнее ответа, ответ главнее статуса заявки.

    Нет ответа: заявка открыта и час брони не прошёл — «ждём»; час прошёл или
    заявку закрыли без ответа (общий путь, API) — «ресторан не ответил».
    """
    if row.cancelled_by_guest_at is not None:
        return BookingStage.CANCELLED
    if row.answer is not None:
        return BookingStage(row.answer.value)
    if status in _OPEN_STATUSES and row.booking_at > now:
        return BookingStage.WAITING
    return BookingStage.NO_ANSWER


async def delete_abandoned_bookings(*, created_before: datetime) -> int:
    """Удалить брони без заявки старше границы — ретеншн, шаг брошенных (Р-3)."""
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(TableBooking).where(
                    TableBooking.created_at < created_before,
                    TableBooking.request_created.is_(False),
                )
            ),
        )
    return result.rowcount or 0


async def detach_expired_bookings(*, created_before: datetime) -> int:
    """Обезличить брони старше границы: связь с проживанием и гостем — NULL (§3).

    Срок — общий с текстом заявки (spec 0032): ресторан, час и число гостей
    остаются, кто именно бронировал, через бронь уже не узнать.
    """
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(TableBooking)
                .where(
                    TableBooking.created_at < created_before,
                    (TableBooking.stay_id.is_not(None))
                    | (TableBooking.guest_identity_id.is_not(None)),
                )
                .values(stay_id=None, guest_identity_id=None)
            ),
        )
    return result.rowcount or 0


def not_found(message: str) -> AppError:
    return AppError(code=ERR_TABLEBOOKING_NOT_FOUND, message=message, status_code=404)


def cancel_not_applicable(booking_id: uuid.UUID) -> AppError:
    return AppError(
        code=ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE,
        message=f"Table booking {booking_id} can no longer be cancelled",
        status_code=409,
    )
