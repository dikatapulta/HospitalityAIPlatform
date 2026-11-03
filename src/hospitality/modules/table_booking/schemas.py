"""Pydantic-схемы модуля table_booking (spec 0064 §4, R-6): вход запроса гостя,
бронь для чтения, варианты формы. Пределы и шаг слотов — константы модуля (§2.6).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hospitality.modules.table_booking.models import BookingAnswer
from hospitality.platform.config import LocalizedText

# Шаг слотов и запас до посадки (§2.2): сегодня — не раньше «сейчас + 30 мин».
SLOT_STEP: Final = timedelta(minutes=30)
LEAD_TIME: Final = timedelta(minutes=30)
# Дальше двух недель вперёд — не через чат (§2.2), и не позже дня выезда.
MAX_DAYS_AHEAD: Final = 14
MAX_GUEST_NAME_LENGTH: Final = 60
MAX_WISHES_LENGTH: Final = 300
# Лимиты проживания (§4, ревью PR #711 Н-6): оба — на проживание, сутки — по поясу отеля.
MAX_UNANSWERED_PER_STAY: Final = 3
MAX_BOOKINGS_PER_HOTEL_DAY: Final = 10

BookingLanguage = Literal["en", "ru", "kk"]


class BookingStage(enum.StrEnum):
    """Стадия брони на странице гостя (§2.2) — выводится, а не хранится.

    «Забронировано» — только `confirmed`: до ответа ресторана это запрос.
    `no_answer` — заявка открыта, а час брони прошёл, или закрыта без ответа.
    """

    WAITING = "waiting"
    CONFIRMED = "confirmed"
    OTHER_TIME = "other_time"
    NO_SEATS = "no_seats"
    CANCELLED = "cancelled"
    NO_ANSWER = "no_answer"


def _strip_or_none(value: object) -> object:
    if isinstance(value, str):
        return value.strip() or None
    return value


class BookingCreate(BaseModel):
    """Запрос гостя на столик: ресторан, дата и время по часам отеля, гости, имя и пожелания."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    restaurant_key: str = Field(min_length=1, max_length=32)
    date: dt.date
    time: dt.time
    party_size: int = Field(ge=1, le=30)
    guest_name: str | None = Field(default=None, max_length=MAX_GUEST_NAME_LENGTH)
    wishes: str | None = Field(default=None, max_length=MAX_WISHES_LENGTH)
    guest_language: BookingLanguage

    @field_validator("guest_name", "wishes", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return _strip_or_none(value)


class BookingRead(BaseModel):
    """Бронь для гостя и подписчика сообщений: час — в UTC и по часам отеля."""

    id: uuid.UUID
    request_id: uuid.UUID
    number: int | None
    restaurant_key: str
    restaurant_names: LocalizedText
    booking_at: datetime
    local_date: dt.date
    local_time: str
    party_size: int
    guest_language: str
    room_number: str | None
    stage: BookingStage
    answer: BookingAnswer | None
    proposed_at: datetime | None
    proposed_time: str | None
    answered_at: datetime | None
    cancelled_by_guest_at: datetime | None
    # Гость ещё может отменить (§2.4): до ответа или после «Подтвердить», до часа брони.
    cancellable: bool
    created_at: datetime


class BookingPlaced(BaseModel):
    """Итог «Отправить запрос»: бронь и признак повтора ключа (200 вместо 201)."""

    booking: BookingRead
    duplicate: bool


class BookingDay(BaseModel):
    """День формы: дата по часам отеля и слоты «ЧЧ:ММ», которые ещё можно запросить."""

    date: dt.date
    slots: list[str]


class RestaurantOption(BaseModel):
    """Ресторан в форме гостя: название, заметка, предел гостей и дни со слотами."""

    key: str
    names: LocalizedText
    note: LocalizedText | None
    max_party: int
    days: list[BookingDay]


class BookingOptions(BaseModel):
    """Что гость может запросить сейчас (§2.2): «сегодня» отеля и рестораны по порядку конфига."""

    today: dt.date
    restaurants: list[RestaurantOption]
