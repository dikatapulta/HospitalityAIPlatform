"""Публичный интерфейс модуля table_booking (spec 0064 §4, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: страница гостя и подписчик сообщений
гостю (`channels/web/table/`), ретеншн, кабинет (PR 3) импортируют ТОЛЬКО отсюда —
остальные файлы модуля приватны (контракт 5 import-linter). Здесь нет логики,
только контракт.
"""

from __future__ import annotations

from hospitality.modules.table_booking.answers import (
    ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE,
    answer_booking,
)
from hospitality.modules.table_booking.bookings import (
    ERR_TABLEBOOKING_LIMIT,
    get_options,
    request_booking,
)
from hospitality.modules.table_booking.models import BookingAnswer
from hospitality.modules.table_booking.schemas import (
    LEAD_TIME,
    MAX_BOOKINGS_PER_HOTEL_DAY,
    MAX_DAYS_AHEAD,
    MAX_GUEST_NAME_LENGTH,
    MAX_UNANSWERED_PER_STAY,
    MAX_WISHES_LENGTH,
    SLOT_STEP,
    BookingCreate,
    BookingDay,
    BookingLanguage,
    BookingOptions,
    BookingPlaced,
    BookingRead,
    BookingStage,
    RestaurantOption,
)
from hospitality.modules.table_booking.slots import (
    ERR_TABLEBOOKING_DISABLED,
    ERR_TABLEBOOKING_INVALID,
)
from hospitality.modules.table_booking.tracking import (
    ABANDONED_AFTER,
    ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE,
    ERR_TABLEBOOKING_NOT_FOUND,
    booking_guest_identity,
    cancel_by_guest,
    delete_abandoned_bookings,
    detach_expired_bookings,
    get_booking_by_request,
    get_booking_for_stay,
    list_bookings_for_stay,
)

__all__ = [
    "ABANDONED_AFTER",
    "ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE",
    "ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE",
    "ERR_TABLEBOOKING_DISABLED",
    "ERR_TABLEBOOKING_INVALID",
    "ERR_TABLEBOOKING_LIMIT",
    "ERR_TABLEBOOKING_NOT_FOUND",
    "LEAD_TIME",
    "MAX_BOOKINGS_PER_HOTEL_DAY",
    "MAX_DAYS_AHEAD",
    "MAX_GUEST_NAME_LENGTH",
    "MAX_UNANSWERED_PER_STAY",
    "MAX_WISHES_LENGTH",
    "SLOT_STEP",
    "BookingAnswer",
    "BookingCreate",
    "BookingDay",
    "BookingLanguage",
    "BookingOptions",
    "BookingPlaced",
    "BookingRead",
    "BookingStage",
    "RestaurantOption",
    "answer_booking",
    "booking_guest_identity",
    "cancel_by_guest",
    "delete_abandoned_bookings",
    "detach_expired_bookings",
    "get_booking_by_request",
    "get_booking_for_stay",
    "get_options",
    "list_bookings_for_stay",
    "request_booking",
]
