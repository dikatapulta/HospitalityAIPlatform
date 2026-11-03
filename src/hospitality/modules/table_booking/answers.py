"""Ответ ресторана на запрос столика (spec 0064 §2.3) — уровень сервиса, кнопки — кабинет (PR 3).

Три ответа — «Подтвердить», «Предложить другое время», «Мест нет». Любой
записывается в бронь и закрывает заявку `done`: работа службы — ответить. Заявку
в `new` ответ сначала берёт на того же сотрудника — два перехода канона
(`new → done` в `STATUS_TRANSITIONS` нет). «Другое время» не подтверждает его:
гость шлёт новый запрос, ресторан подтверждает его одним нажатием.

Порядок — как отмена заказа сотрудником (spec 0038): ответ пишется под
`FOR UPDATE` брони, затем переходы заявки; отказ перехода снимает ответ, если в
строке всё ещё он. Отмена гостем берёт ту же блокировку — выигрывает один.
Ответ не зависит от флага брони и наличия ресторана в конфиге (ревью PR #711,
Н-3): открытые запросы закрываются и после выключения; «Другое время» без
ресторана в конфиге — 002 (часов не знаем).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime, time
from typing import Final

from sqlalchemy import select, update

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking.models import BookingAnswer, TableBooking
from hospitality.modules.table_booking.schemas import BookingRead
from hospitality.modules.table_booking.slots import check_proposed_moment, invalid, load_config
from hospitality.modules.table_booking.tracking import not_found, read_bookings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_table_booking

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE: Final = "ERR-TABLEBOOKING-005"


async def answer_booking(
    request_id: uuid.UUID,
    answer: BookingAnswer,
    proposed_time: time | None = None,
    *,
    acting_user: requests_api.ActingUser,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> BookingRead:
    """Ответ ресторана на запрос (§2.3, §4); `proposed_time` — час «Другого времени»
    по часам отеля, того же дня, что запрос.

    Повтор того же ответа (и того же часа) — без изменений. Иной ответ на
    отвеченный, отменённый гостем, ответ после часа брони (Н-2) или по закрытой
    заявке — 005. Заявка другой службы — ERR-REQUESTS-007 (403) раньше записи.
    «Другое время» без часа, с часом вне слотов ресторана или ближе «сейчас +
    30 мин» — 002; час у иного ответа — 002.
    """
    if (answer is BookingAnswer.OTHER_TIME) != (proposed_time is not None):
        raise invalid("proposed_time is required for other_time and only for it")
    row = await _booking_of_request(request_id)
    request = await requests_api.get_request(request_id)
    if allowed_category_ids is not None and request.category_id not in allowed_category_ids:
        # Чужой службе отказ не рассказывает, что это бронь и в какой она стадии.
        raise AppError(
            code=requests_api.ERR_REQUESTS_FOREIGN_SERVICE,
            message=f"Request {request_id} belongs to another service",
            status_code=403,
        )
    proposed_at = await _proposed_moment(row, proposed_time) if proposed_time else None
    written = await _write_answer(row.id, answer, proposed_at, acting_user)
    if written is None:
        return (await read_bookings([row]))[0]  # тот же ответ — без изменений
    try:
        await _close_request(request_id, acting_user, allowed_category_ids)
    except AppError as error:
        await _withdraw_answer(row.id, written)
        if error.code == requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION:
            raise not_applicable(request_id, "the request is already closed") from error
        raise
    record_table_booking(answer.value)
    logger.info(
        "table_booking_answered",
        booking_id=str(row.id),
        request_id=str(request_id),
        answer=answer.value,
    )
    return (await read_bookings([await _booking_of_request(request_id)]))[0]


async def _booking_of_request(request_id: uuid.UUID) -> TableBooking:
    async with session_scope() as session:
        row = await session.scalar(
            select(TableBooking).where(TableBooking.request_id == request_id)
        )
    if row is None or not row.request_created:
        raise not_found(f"Request {request_id} is not a table booking")
    return row


async def _proposed_moment(row: TableBooking, proposed_time: time) -> datetime:
    """Час «Другого времени» в UTC: слот того же дня по часам ресторана (Н-2, Н-3)."""
    config = await load_config()
    booking = config.table_booking
    restaurant = booking.restaurant(row.restaurant_key) if booking else None
    if restaurant is None:
        raise invalid(f"Restaurant {row.restaurant_key!r} is not in the config: hours unknown")
    day = row.booking_at.astimezone(config.tzinfo).date()
    return check_proposed_moment(restaurant, day, proposed_time, zone=config.tzinfo, now=utc_now())


async def _write_answer(
    booking_id: uuid.UUID,
    answer: BookingAnswer,
    proposed_at: datetime | None,
    acting_user: requests_api.ActingUser,
) -> datetime | None:
    """Ответ под `FOR UPDATE`; вернуть его момент — или None, если это повтор того же."""
    now = utc_now()
    async with session_scope() as session:
        row = await session.scalar(
            select(TableBooking).where(TableBooking.id == booking_id).with_for_update()
        )
        assert row is not None  # строку брони держит её заявка
        if row.cancelled_by_guest_at is not None:
            raise not_applicable(row.request_id, "the guest cancelled the request")
        if row.answer is not None:
            if row.answer is answer and row.proposed_at == proposed_at:
                return None
            raise not_applicable(row.request_id, f"already answered {row.answer.value!r}")
        if row.booking_at <= now:
            raise not_applicable(row.request_id, "the booking hour has passed")
        row.answer = answer
        row.proposed_at = proposed_at
        row.answered_at = now
        row.answered_by_user_id = acting_user.user_id
        row.answered_by_display_name = acting_user.display_name
    return now


async def _close_request(
    request_id: uuid.UUID,
    acting_user: requests_api.ActingUser,
    allowed_category_ids: Collection[uuid.UUID] | None,
) -> None:
    """Невзятую — взять на того же сотрудника, затем `done`: два перехода канона."""
    status = requests_api.RequestStatus
    request = await requests_api.get_request(request_id)
    if request.status is status.NEW:
        try:
            await requests_api.change_request_status(
                request_id,
                status.IN_PROGRESS,
                initiator=requests_api.RequestInitiator.STAFF,
                acting_user=acting_user,
                allowed_category_ids=allowed_category_ids,
                only_from=status.NEW,
            )
        except AppError as error:
            # Взяли в ту же секунду другим путём — закрыть всё равно можно.
            if error.code != requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION:
                raise
    await requests_api.change_request_status(
        request_id,
        status.DONE,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=acting_user,
        allowed_category_ids=allowed_category_ids,
    )


async def _withdraw_answer(booking_id: uuid.UUID, written_at: datetime) -> None:
    """Снять ответ проигравшего перехода — только если в строке всё ещё он."""
    async with session_scope() as session:
        await session.execute(
            update(TableBooking)
            .where(TableBooking.id == booking_id, TableBooking.answered_at == written_at)
            .values(
                answer=None,
                proposed_at=None,
                answered_at=None,
                answered_by_user_id=None,
                answered_by_display_name=None,
            )
        )


def not_applicable(request_id: uuid.UUID, reason: str) -> AppError:
    return AppError(
        code=ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE,
        message=f"Cannot answer table booking request {request_id}: {reason}",
        status_code=409,
    )
