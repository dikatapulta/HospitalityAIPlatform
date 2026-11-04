"""Схемы запроса из 3D-витрины (spec 0069 §2.4, §4; PR (в), #805) — граница модуля (R-6).

Пределы формы — здесь, числами спеки: группа 1–500 гостей и до 60 номеров,
бронирование 1–`booking_max_guests` (конфиг) и до 3 номеров, заезд не раньше
сегодня отеля и выезд не дальше 400 дней. Что номер, вид и тип — из сцены отеля,
проверяет сервис (`visitor_requests.py`): схема сцены не знает.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hospitality.modules.showroom.request_models import ShowroomRequestKind
from hospitality.platform.config import LocalizedText

MAX_GROUP_GUESTS: Final = 500
MAX_GROUP_ROOMS: Final = 60
MAX_BOOKING_ROOMS: Final = 3
MAX_DAYS_AHEAD: Final = 400
MAX_WISHES_LENGTH: Final = 500
# Срок контактов, компании и пожеланий (§3): сделка по группе живёт месяцами.
CONTACTS_RETENTION_DAYS: Final = 365

ShowroomLanguage = Literal["en", "ru", "kk"]

# Телефон — как его пишут люди: «+7 (701) 123-45-67»; не меньше семи цифр.
_PHONE_PATTERN: Final = re.compile(r"^\+?[0-9 ()\-]{7,32}$")
# Почта — без внешних зависимостей: «что-то@что-то.что-то», без пробелов.
_EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_KEY_PATTERN: Final = r"^[a-z0-9_-]{1,64}$"


def _clean(value: object) -> object:
    """Пробелы по краям срезаются, пустая строка — «не указано»."""
    if isinstance(value, str):
        return value.strip() or None
    return value


class ShowroomRequestCreate(BaseModel):
    """Тело «Отправить запрос» обеих форм (§2.4) — поля посетителя, без согласия.

    Согласие — строка под кнопкой (§2.6): отправка формы и есть согласие, его
    версию передаёт канал (`create_request(…, consent_version=…)`).
    """

    model_config = ConfigDict(extra="forbid")

    client_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: ShowroomRequestKind
    company: str | None = Field(default=None, max_length=200)
    contact_name: str = Field(min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    guests_count: int = Field(ge=1, le=MAX_GROUP_GUESTS)
    check_in: date
    check_out: date
    room_numbers: list[str] = Field(default_factory=list, max_length=MAX_GROUP_ROOMS)
    wish_view_key: str | None = Field(default=None, pattern=_KEY_PATTERN)
    wish_type_key: str | None = Field(default=None, pattern=_KEY_PATTERN)
    wishes: str | None = Field(default=None, max_length=MAX_WISHES_LENGTH)
    language: ShowroomLanguage

    @field_validator("company", "contact_name", "phone", "email", "wishes", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return _clean(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        if value is not None and (
            not _PHONE_PATTERN.match(value) or sum(char.isdigit() for char in value) < 7
        ):
            raise ValueError("Phone must be digits, spaces, +, (, ) or -, at least 7 digits")
        return value

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL_PATTERN.match(value):
            raise ValueError("Email must look like name@example.com")
        return value

    @field_validator("room_numbers")
    @classmethod
    def _rooms(cls, value: list[str]) -> list[str]:
        cleaned = [number.strip() for number in value]
        if any(not number or len(number) > 20 for number in cleaned):
            raise ValueError("Room numbers must be 1–20 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Room numbers must not repeat")
        return cleaned

    @model_validator(mode="after")
    def _kind_rules(self) -> ShowroomRequestCreate:
        """Служба отвечает телефоном или почтой — хоть одно обязано быть; у группы — компания."""
        if self.phone is None and self.email is None:
            raise ValueError("Phone or email is required: the hotel answers by one of them")
        if self.kind is ShowroomRequestKind.GROUP and self.company is None:
            raise ValueError("Company is required for a group request")
        if self.kind is ShowroomRequestKind.BOOKING and len(self.room_numbers) > MAX_BOOKING_ROOMS:
            raise ValueError(f"A booking request names at most {MAX_BOOKING_ROOMS} rooms")
        return self


class ShowroomRequestRead(BaseModel):
    """Запрос после записи — каналу: посетителю он не отдаётся, только «отправлено»."""

    id: uuid.UUID
    request_id: uuid.UUID
    kind: ShowroomRequestKind
    created_at: datetime


class ShowroomRequestPlaced(BaseModel):
    """Итог «Отправить запрос»: повтор того же `client_key` — `duplicate` (200, не 201)."""

    request: ShowroomRequestRead
    duplicate: bool


class StaffShowroomRequestRead(BaseModel):
    """Запрос для карточки кабинета (§5): контакты — отсюда, а не из текста заявки.

    После обезличивания (365 дней) контакты, компания и пожелания — None,
    `anonymized` — True.
    """

    request_id: uuid.UUID
    kind: ShowroomRequestKind
    company: str | None
    contact_name: str | None
    phone: str | None
    email: str | None
    guests_count: int
    check_in: date
    check_out: date
    room_numbers: list[str]
    wish_view_names: LocalizedText | None
    wish_type_names: LocalizedText | None
    wishes: str | None
    language: str
    anonymized: bool
