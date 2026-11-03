"""Запрос гостя на столик (spec 0064 §2.1–2.2, §4) — копия канона `room_service/orders.py`.

Бронь — ресторан, час и гости модуля плюс заявка в службе ресторана (§2.1): номер
`#N`, очередь, сигнал, пуш и лестница невзятой заявки — у заявки. Запись —
порядком Р-3 spec 0038: строка брони с `request_id = uuid4()` →
`requests.create_request(request_id=…)`, идемпотентный по id → отметка
`request_created`. Сбой между шагами — бронь без заявки: её не видит никто,
повтор «Отправить запрос» тем же ключом доводит дело, брошенную удаляет ретеншн
через сутки. Модель брони не создаёт и не видит: заявки нет в `request_origins`.

Каждая функция вызывается внутри `tenant_context`. Чья это сессия гостя и какое
проживание — решает канал; домен перепроверяет, что проживание активно.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, time, tzinfo
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking.models import TableBooking
from hospitality.modules.table_booking.schemas import (
    MAX_BOOKINGS_PER_HOTEL_DAY,
    MAX_UNANSWERED_PER_STAY,
    BookingCreate,
    BookingOptions,
    BookingPlaced,
    BookingRead,
    RestaurantOption,
)
from hospitality.modules.table_booking.slots import (
    booking_days,
    check_booking_moment,
    disabled,
    hotel_today,
    invalid,
    load_config,
    open_restaurants,
    ru_day,
)
from hospitality.modules.table_booking.tracking import not_found, read_bookings
from hospitality.platform.config import LocalizedText
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_table_booking
from hospitality.shared.pii import mask_payment_card_numbers
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_TABLEBOOKING_LIMIT: Final = "ERR-TABLEBOOKING-003"

_CLIENT_KEY_CONSTRAINT: Final = "uq_table_bookings_client_key"


async def get_options(stay_id: uuid.UUID) -> BookingOptions:
    """Рестораны, дни и слоты, которые гость может запросить сейчас (§2.2).

    Бронь выключена или ресторанов нет — 001; проживание не активно — 004.
    """
    booking, config = await open_restaurants()
    stay = await guests_api.get_active_stay(stay_id)
    if stay is None:
        raise not_found(f"Stay {stay_id} is not active")
    now = utc_now()
    return BookingOptions(
        today=hotel_today(config.tzinfo, now),
        restaurants=[
            RestaurantOption(
                key=restaurant.key,
                names=restaurant.names,
                note=restaurant.note,
                max_party=restaurant.max_party,
                days=booking_days(
                    restaurant, zone=config.tzinfo, now=now, check_out_at=stay.check_out_at
                ),
            )
            for restaurant in booking.restaurants
        ],
    )


async def request_booking(
    stay_id: uuid.UUID, guest_identity_id: uuid.UUID, data: BookingCreate
) -> BookingPlaced:
    """«Отправить запрос»: проверки, строка брони, заявка в службу ресторана.

    Повтор тем же `client_key` у того же проживания — та же бронь
    (`duplicate=True`), и заявка доводится, если в прошлый раз не успела.
    Порядок отказов: проживание (004), бронь выключена или ресторана нет (001),
    гости, день, слот или запас (002), лимит проживания (003).
    """
    existing = await _find_by_client_key(stay_id, data.client_key)
    if existing is not None:
        return BookingPlaced(booking=await _ensure_request(existing, data), duplicate=True)
    stay = await guests_api.get_active_stay(stay_id)
    if stay is None:
        raise not_found(f"Stay {stay_id} is not active")
    booking, config = await open_restaurants()
    restaurant = booking.restaurant(data.restaurant_key)
    if restaurant is None:
        raise disabled(f"Restaurant {data.restaurant_key!r} is not in the hotel config")
    if data.party_size > restaurant.max_party:
        raise invalid(f"Party of {data.party_size} is over {restaurant.max_party}")
    now = utc_now()
    booking_at = check_booking_moment(
        restaurant,
        data.date,
        data.time,
        zone=config.tzinfo,
        now=now,
        check_out_at=stay.check_out_at,
    )
    await _check_limits(stay.id, _hotel_day_start(config.tzinfo, now))
    category_id = await _category_id(restaurant.category_key)

    row = TableBooking(
        id=uuid.uuid4(),
        tenant_id=current_tenant_id(),
        request_id=uuid.uuid4(),
        request_created=False,
        stay_id=stay.id,
        guest_identity_id=guest_identity_id,
        client_key=data.client_key,
        restaurant_key=restaurant.key,
        restaurant_names=restaurant.names.as_json(),
        booking_at=booking_at,
        party_size=data.party_size,
        guest_language=data.guest_language,
    )
    try:
        async with session_scope() as session:
            session.add(row)
    except IntegrityError as error:
        # Двойное «Отправить» в одну секунду: второй проигрывает ключу и отдаёт бронь первого.
        if _CLIENT_KEY_CONSTRAINT not in str(error):
            raise
        winner = await _find_by_client_key(stay_id, data.client_key)
        if winner is None:  # pragma: no cover — нарушение ключа без строки невозможно
            raise
        return BookingPlaced(booking=await _ensure_request(winner, data), duplicate=True)
    placed = await _ensure_request(row, data, category_id=category_id)
    record_table_booking("requested")
    logger.info(
        "table_booking_requested",
        booking_id=str(row.id),
        request_id=str(row.request_id),
        restaurant_key=row.restaurant_key,
        party_size=row.party_size,
    )
    return BookingPlaced(booking=placed, duplicate=False)


async def _check_limits(stay_id: uuid.UUID, day_start: datetime) -> None:
    """Лимиты проживания (§4, Н-6): без ответа ≤ 3, за сутки отеля ≤ 10 — иначе 003.

    В счёт суток идут и отменённые: лимит — от потока запросов, а не от броней.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    TableBooking.request_id,
                    TableBooking.created_at,
                    TableBooking.answer,
                    TableBooking.cancelled_by_guest_at,
                ).where(TableBooking.stay_id == stay_id, TableBooking.request_created)
            )
        ).all()
    today = sum(1 for row in rows if row.created_at >= day_start)
    waiting = [
        row.request_id for row in rows if row.answer is None and row.cancelled_by_guest_at is None
    ]
    unanswered = await requests_api.list_open_requests_by_ids(waiting)
    if today >= MAX_BOOKINGS_PER_HOTEL_DAY or len(unanswered) >= MAX_UNANSWERED_PER_STAY:
        logger.warning("table_booking_limited", unanswered=len(unanswered), today=today)
        raise AppError(
            code=ERR_TABLEBOOKING_LIMIT,
            message="Too many table booking requests for this stay",
            status_code=429,
        )


def _hotel_day_start(zone: tzinfo, now: datetime) -> datetime:
    """Начало суток отеля «сегодня» — граница суточного лимита (как у заказа в номер)."""
    return datetime.combine(now.astimezone(zone).date(), time.min, tzinfo=zone)


async def _category_id(category_key: str) -> uuid.UUID:
    for category in await requests_api.list_categories():
        if category.key == category_key:
            return category.id
    # Онбординг сверяет службу ресторана с категориями отеля; без неё запрос некуда отдать.
    raise disabled(f"Table booking category {category_key!r} does not exist")


async def _find_by_client_key(stay_id: uuid.UUID, client_key: str) -> TableBooking | None:
    async with session_scope() as session:
        found: TableBooking | None = await session.scalar(
            select(TableBooking).where(
                TableBooking.stay_id == stay_id, TableBooking.client_key == client_key
            )
        )
    return found


async def _ensure_request(
    row: TableBooking, data: BookingCreate, *, category_id: uuid.UUID | None = None
) -> BookingRead:
    """Заявка брони (идемпотентно по `request_id`) и отметка «заявка есть»."""
    if not row.request_created:
        config = await load_config()
        if category_id is None:
            booking = config.table_booking
            restaurant = booking.restaurant(row.restaurant_key) if booking else None
            if restaurant is None:
                raise disabled(f"Restaurant {row.restaurant_key!r} is not in the hotel config")
            category_id = await _category_id(restaurant.category_key)
        stay = await guests_api.get_active_stay(row.stay_id) if row.stay_id else None
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_FORM,
                summary=_request_summary(row, config.tzinfo),
                details=_request_details(data.guest_name, data.wishes),
                room_number=stay.room_number if stay is not None else None,
                guest_language=row.guest_language,
            ),
            request_id=row.request_id,
            stay_id=row.stay_id,
        )
        async with session_scope() as session:
            await session.execute(
                update(TableBooking).where(TableBooking.id == row.id).values(request_created=True)
            )
        row.request_created = True
    return (await read_bookings([row]))[0]


def _request_summary(row: TableBooking, zone: tzinfo) -> str:
    """Суть заявки для службы ресторана, по-русски (текст заявки — срок spec 0032)."""
    name = LocalizedText.model_validate(row.restaurant_names).text_for("ru")
    local = row.booking_at.astimezone(zone)
    return f"Столик: «{name}», {ru_day(local.date())}, {local:%H:%M}, гостей — {row.party_size}"


def _request_details(guest_name: str | None, wishes: str | None) -> str | None:
    """Имя и пожелания — `details` заявки: обезличиваются с ней (§3); номер карты маскируется."""
    lines: Sequence[str] = [
        line
        for line in (
            f"На имя: {guest_name}" if guest_name else None,
            f"Пожелания: {wishes}" if wishes else None,
        )
        if line is not None
    ]
    return mask_payment_card_numbers("\n".join(lines)) if lines else None
