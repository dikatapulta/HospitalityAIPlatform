"""Pydantic-схемы HTTP-границы страницы «Забронировать столик» (spec 0064 §4, R-6, P-7).

Проживание и идентичность гостя в теле не ходят — их берёт маршрут из сессии.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.table_booking import api as table_booking_api


class TableData(BaseModel):
    """Данные страницы: комната из сессии, «сегодня» отеля, рестораны со слотами, брони."""

    room_number: str
    today: dt.date
    restaurants: list[table_booking_api.RestaurantOption]
    bookings: list[table_booking_api.BookingRead]


class BookingBody(BaseModel):
    """Тело «Отправить запрос» (`POST …/table/bookings`)."""

    model_config = ConfigDict(extra="forbid")

    client_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    restaurant_key: str = Field(min_length=1, max_length=32)
    date: dt.date
    time: dt.time
    party_size: int = Field(ge=1, le=30)
    guest_name: str | None = Field(default=None, max_length=table_booking_api.MAX_GUEST_NAME_LENGTH)
    wishes: str | None = Field(default=None, max_length=table_booking_api.MAX_WISHES_LENGTH)
    language: table_booking_api.BookingLanguage
