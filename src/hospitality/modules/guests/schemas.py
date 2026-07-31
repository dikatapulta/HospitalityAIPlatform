"""Pydantic-схемы границ модуля guests (spec 0027 §1.4, R-6, P-7).

Сервисные функции принимают `*Start`/`*CheckIn` и возвращают `*Read`/`*Grant` —
ORM-объекты наружу модуля не выходят. `tenant_id` в схемах отсутствует
намеренно: тенанта задаёт контекст (P-4), вызывающая сторона его не выбирает.

Plaintext-секреты (`access_code`, `session_token`) живут ТОЛЬКО в результатах
операций, которые их порождают (`StayCheckInResult`, `GuestSessionGrant`), —
показываются один раз и нигде больше не читаются (в БД — хэши, ADR-008).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.guests.models import GuestIdentityKind, StayStatus


class StayCheckIn(BaseModel):
    """Данные заселения (CLI и кнопка «Заселить» кабинета, spec 0033 §6)."""

    room_number: str = Field(min_length=1, max_length=20)
    # UTC (§9); перевод «12:00 по времени отеля» → UTC — забота вызывающего.
    check_out_at: datetime
    guest_display_name: str | None = Field(default=None, max_length=255)
    # Кнопки «1/2/3+» кабинета («3+» хранится как 3); вход квот #124.
    guests_count: int = Field(default=1, ge=1, le=99)


class StayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    guest_id: uuid.UUID
    room_number: str
    status: StayStatus
    guests_count: int
    check_in_at: datetime
    check_out_at: datetime


class StayCheckInResult(BaseModel):
    """Итог заселения: Stay + код заселения ОДИН раз в открытом виде (§1.2)."""

    stay: StayRead
    access_code: str


class GuestSessionStart(BaseModel):
    """Запрос привязки канала к Stay: тройка тенант+комната+код (ADR-008 §3).

    Тенант — из контекста (P-4). `identity_external_id` — идентификатор
    клиента в канале (web: UUID, порождённый каналом; telegram: chat_id —
    когда Telegram перейдёт на auth-only). `consent_version` — версия текста
    согласия, показанного при привязке (юраудит 22.07).
    """

    room_number: str = Field(min_length=1, max_length=20)
    code: str = Field(min_length=1, max_length=32)
    identity_kind: GuestIdentityKind = GuestIdentityKind.WEB
    identity_external_id: str = Field(min_length=1, max_length=128)
    consent_version: str = Field(min_length=1, max_length=16)


class GuestSessionBind(BaseModel):
    """Запрос привязки по потреблённой bind-ссылке (spec 0033 §6).

    Право на Stay дала одноразовая ссылка, выпущенная персоналом
    (`bindlink.consume_bind_link` уже вернул `stay_id`), — комнаты и кода
    здесь нет. Остальные поля — те же, что у `GuestSessionStart`: привязка
    создаёт идентичность и сессию тем же путём (P-12).
    """

    stay_id: uuid.UUID
    identity_kind: GuestIdentityKind = GuestIdentityKind.WEB
    identity_external_id: str = Field(min_length=1, max_length=128)
    consent_version: str = Field(min_length=1, max_length=16)


class GuestSessionGrant(BaseModel):
    """Успешная привязка: opaque-токен сессии ОДИН раз в открытом виде."""

    session_token: str
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    room_number: str


class ActiveGuestSession(BaseModel):
    """Валидная сессия на текущем действии (`resolve_session`, ADR-008 §3)."""

    session_id: uuid.UUID
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    room_number: str
    check_out_at: datetime
