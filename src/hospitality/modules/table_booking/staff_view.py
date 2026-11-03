"""Бронь для кабинета (spec 0064 §2.3, §5): карточка в очереди службы и метка уведомления.

Очередь и сигнал кабинета уже прочитали заявки — здесь к ним добавляются брони,
одним запросом, без перечитывания заявок (канон `room_service.orders_for_requests`).
Заявка «Гость отменил бронь» бронью не считается: у неё обычная карточка и
обычное «Готово» (§2.4). Бронь без заявки не видна. Недоведённую заявку отмены
доводит показ брони гостю, а не кабинет: поллинг очереди ничего не пишет.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import select

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking.models import TableBooking
from hospitality.modules.table_booking.schemas import LEAD_TIME, BookingStage, StaffBookingRead
from hospitality.modules.table_booking.slots import (
    load_config,
    local_moment,
    ru_day,
    slot_times,
)
from hospitality.modules.table_booking.tracking import OPEN_STATUSES, read_booking
from hospitality.platform.config import TenantConfig
from hospitality.shared.db import session_scope, utc_now


async def bookings_for_requests(
    requests: Sequence[requests_api.ServiceRequestRead],
) -> dict[uuid.UUID, StaffBookingRead]:
    """Брони уже прочитанных заявок; ключ — `request_id`, заявка не бронь — её в словаре нет."""
    by_id = {request.id: request for request in requests}
    rows = await _rows(by_id.keys())
    if not rows:
        return {}
    config = await load_config()
    now = utc_now()
    return {row.request_id: _staff_read(row, by_id[row.request_id], config, now) for row in rows}


async def booking_request_ids(request_ids: Collection[uuid.UUID]) -> frozenset[uuid.UUID]:
    """Какие из заявок — запросы столика: уведомлению «Запрос на столик #12» (§5)."""
    return frozenset(row.request_id for row in await _rows(request_ids))


async def _rows(request_ids: Collection[uuid.UUID]) -> list[TableBooking]:
    if not request_ids:
        return []
    async with session_scope() as session:
        rows = await session.scalars(
            select(TableBooking).where(
                TableBooking.request_id.in_(list(request_ids)), TableBooking.request_created
            )
        )
        return list(rows.all())


def _staff_read(
    row: TableBooking,
    request: requests_api.ServiceRequestRead,
    config: TenantConfig,
    now: datetime,
) -> StaffBookingRead:
    zone = config.tzinfo
    booking = read_booking(row, request, zone, now)
    is_open = request.status in OPEN_STATUSES
    return StaffBookingRead(
        **booking.model_dump(),
        answered_by_display_name=row.answered_by_display_name,
        day_label=ru_day(booking.local_date),
        can_answer=booking.stage is BookingStage.WAITING,
        can_close_unanswered=is_open and booking.stage is BookingStage.NO_ANSWER,
        other_time_slots=(
            _other_time_slots(row, config, now) if booking.stage is BookingStage.WAITING else []
        ),
    )


def _other_time_slots(row: TableBooking, config: TenantConfig, now: datetime) -> list[str]:
    """Слоты «Другого времени»: тот же день по часам ресторана, кроме запрошенного часа.

    Ресторана в конфиге уже нет — часов не знаем, список пуст (ответ 002 был бы
    тем же). Ближе «сейчас + 30 мин» не предлагается — так проверяет и ответ.
    """
    booking = config.table_booking
    restaurant = booking.restaurant(row.restaurant_key) if booking else None
    if restaurant is None:
        return []
    zone = config.tzinfo
    day = row.booking_at.astimezone(zone).date()
    return [
        at.strftime("%H:%M")
        for at in slot_times(restaurant)
        if (moment := local_moment(day, at, zone)) >= now + LEAD_TIME and moment != row.booking_at
    ]
