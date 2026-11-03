"""Часы и слоты брони (spec 0064 §2.2): что гость вправе запросить и что ресторан вправе предложить.

Всё время — по поясу отеля (`TenantConfig.tzinfo`, FOUNDATION §9): часы посадки в
конфиге местные, в БД — UTC. Слот — `first_seating + k × 30 мин`, не позже
`last_seating`; сегодня — не раньше «сейчас + 30 мин». Дни — от сегодня отеля до
дня выезда включительно, не дальше 14 дней.

Здесь же — чтение конфига брони и коды 001–002: их проверяют и запрос гостя, и ответ
ресторана.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Final

from hospitality.modules.table_booking.schemas import (
    LEAD_TIME,
    MAX_DAYS_AHEAD,
    SLOT_STEP,
    BookingDay,
)
from hospitality.platform.config import TenantConfig, load_tenant_config
from hospitality.platform.table_booking_config import RestaurantConfig, TableBookingConfig
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import current_tenant_id

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_TABLEBOOKING_DISABLED: Final = "ERR-TABLEBOOKING-001"
ERR_TABLEBOOKING_INVALID: Final = "ERR-TABLEBOOKING-002"


async def load_config() -> TenantConfig:
    """Конфиг тенанта из контекста (P-4)."""
    async with session_scope() as session:
        return await load_tenant_config(session, current_tenant_id())


async def open_restaurants() -> tuple[TableBookingConfig, TenantConfig]:
    """Бронь отеля, если она включена и в ней есть ресторан; иначе 001 (404)."""
    config = await load_config()
    booking = config.table_booking
    if booking is None or not booking.is_open:
        raise disabled("Table booking is disabled")
    return booking, config


def disabled(message: str) -> AppError:
    return AppError(code=ERR_TABLEBOOKING_DISABLED, message=message, status_code=404)


def invalid(message: str) -> AppError:
    return AppError(code=ERR_TABLEBOOKING_INVALID, message=message, status_code=422)


def slot_times(restaurant: RestaurantConfig) -> list[time]:
    """Слоты ресторана за день по шагу 30 мин — местное время."""
    first = _minutes(restaurant.first_seating)
    last = _minutes(restaurant.last_seating)
    step = int(SLOT_STEP.total_seconds() // 60)
    return [time(minute // 60, minute % 60) for minute in range(first, last + 1, step)]


def hotel_today(zone: tzinfo, now: datetime) -> date:
    return now.astimezone(zone).date()


def last_bookable_day(zone: tzinfo, now: datetime, check_out_at: datetime) -> date:
    """Последний день формы: день выезда по часам отеля, но не дальше 14 дней."""
    limit = hotel_today(zone, now) + timedelta(days=MAX_DAYS_AHEAD)
    return min(check_out_at.astimezone(zone).date(), limit)


def booking_days(
    restaurant: RestaurantConfig, *, zone: tzinfo, now: datetime, check_out_at: datetime
) -> list[BookingDay]:
    """Дни и слоты формы: дни без слотов (сегодня после последней посадки) не попадают."""
    today = hotel_today(zone, now)
    last = last_bookable_day(zone, now, check_out_at)
    earliest = now + LEAD_TIME
    days: list[BookingDay] = []
    day = today
    while day <= last:
        slots = [
            at.strftime("%H:%M")
            for at in slot_times(restaurant)
            if local_moment(day, at, zone) >= earliest
        ]
        if slots:
            days.append(BookingDay(date=day, slots=slots))
        day += timedelta(days=1)
    return days


def check_booking_moment(
    restaurant: RestaurantConfig,
    day: date,
    at: time,
    *,
    zone: tzinfo,
    now: datetime,
    check_out_at: datetime,
) -> datetime:
    """Час запроса гостя в UTC; день, слот или запас вне правил — 002."""
    if not hotel_today(zone, now) <= day <= last_bookable_day(zone, now, check_out_at):
        raise invalid(f"Booking date {day} is outside the stay or the 14-day window")
    return _slot_moment(restaurant, day, at, zone=zone, now=now)


def check_proposed_moment(
    restaurant: RestaurantConfig, day: date, at: time, *, zone: tzinfo, now: datetime
) -> datetime:
    """Час «Другого времени» в UTC: слот того же дня и не раньше «сейчас + 30 мин» (Н-2)."""
    return _slot_moment(restaurant, day, at, zone=zone, now=now)


def _slot_moment(
    restaurant: RestaurantConfig, day: date, at: time, *, zone: tzinfo, now: datetime
) -> datetime:
    if at.replace(second=0, microsecond=0, tzinfo=None) not in slot_times(restaurant):
        raise invalid(f"{at:%H:%M} is not a seating slot of {restaurant.key!r}")
    moment = local_moment(day, at, zone)
    if moment < now + LEAD_TIME:
        raise invalid(f"{day} {at:%H:%M} is less than 30 minutes from now")
    return moment


def local_moment(day: date, at: time, zone: tzinfo) -> datetime:
    """Местные дата и время отеля → aware-момент (сравнивается с UTC корректно)."""
    return datetime.combine(day, at.replace(tzinfo=None), tzinfo=zone)


def local_clock(moment: datetime, zone: tzinfo) -> str:
    """«ЧЧ:ММ» по часам отеля."""
    return moment.astimezone(zone).strftime("%H:%M")


_RU_MONTHS: Final = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def ru_day(day: date) -> str:
    """«5 ноября» — дата в сути заявки для службы (заявки пишутся по-русски)."""
    return f"{day.day} {_RU_MONTHS[day.month - 1]}"


def _minutes(at: time) -> int:
    return at.hour * 60 + at.minute
