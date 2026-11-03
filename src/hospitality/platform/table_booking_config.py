"""Бронь столика: рестораны отеля и их часы посадки (spec 0064 §2.6, #702).

Данные тенанта (P-11, NG-7): какие рестораны принимают запросы из веб-чата, какая
служба отвечает за каждый, с какого по какой час сажают гостей и сколько гостей
в одной брони. Поле `TenantConfig.table_booking`; отдельный файл, а не ещё один
класс в `config.py`: тот за границей R-3 (как `service_quotas.py`).

Что служба каждого ресторана — категория заявок отеля, сверяет онбординг
(`tools/onboard_tenant`): категории живут в БД, а не в конфиге.
"""

from __future__ import annotations

from datetime import time
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hospitality.platform.config_values import CATEGORY_KEY_PATTERN, LocalizedText

# Ресторанов у отеля — не больше (§2.6); у пилота их четыре.
MAX_RESTAURANTS: Final = 10
# Предел гостей одной брони: больше — компания, её ресторан бронирует сам (§5).
MAX_PARTY_LIMIT: Final = 30


class RestaurantConfig(BaseModel):
    """Ресторан отеля: служба, что отвечает на запрос, и часы посадки по поясу отеля.

    `last_seating` — последняя посадка того же дня: ресторан, работающий за
    полночь, задаёт её до 23:59 (часы через полночь — запись в docs/later/).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    # `en` обязателен: его видит гость без перевода (Р-6 spec 0038).
    names: LocalizedText
    # «2 этаж, европейская кухня» — под названием в форме гостя.
    note: LocalizedText | None = None
    category_key: str = Field(pattern=CATEGORY_KEY_PATTERN.pattern, max_length=63)
    first_seating: time
    last_seating: time
    max_party: int = Field(default=8, ge=1, le=MAX_PARTY_LIMIT)

    @field_validator("first_seating", "last_seating")
    @classmethod
    def _seating_is_local_wall_clock(cls, value: time) -> time:
        # Часы — местное время отеля; пояс задаёт `TenantConfig.timezone`, не поле.
        if value.tzinfo is not None:
            raise ValueError("seating time must be local, without a timezone")
        return value.replace(second=0, microsecond=0)

    @model_validator(mode="after")
    def _last_seating_not_before_first(self) -> RestaurantConfig:
        if self.last_seating < self.first_seating:
            raise ValueError(
                f"restaurant {self.key!r}: last_seating {self.last_seating} is before "
                f"first_seating {self.first_seating} (same day only)"
            )
        return self


class TableBookingConfig(BaseModel):
    """Бронь столика у отеля (§2.6): флаг и рестораны.

    `enabled=False` или пустой список — маршрутов гостя нет (404
    ERR-TABLEBOOKING-001) и кнопки в чате нет. Ответ ресторана из кабинета от
    флага не зависит (ревью PR #711, Н-3): открытые запросы закрываются и после
    выключения.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    restaurants: tuple[RestaurantConfig, ...] = Field(default=(), max_length=MAX_RESTAURANTS)

    @field_validator("restaurants")
    @classmethod
    def _restaurant_keys_must_be_unique(
        cls, value: tuple[RestaurantConfig, ...]
    ) -> tuple[RestaurantConfig, ...]:
        keys = [restaurant.key for restaurant in value]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate restaurant keys: {duplicates!r}")
        return value

    def restaurant(self, key: str) -> RestaurantConfig | None:
        """Ресторан по ключу; нет такого — None."""
        return next((r for r in self.restaurants if r.key == key), None)

    @property
    def is_open(self) -> bool:
        """Принимает ли отель запросы гостей: включено и есть хотя бы один ресторан."""
        return self.enabled and bool(self.restaurants)
