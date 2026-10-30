"""Квоты бесплатного: норма отеля на проживание в день и платная альтернатива (spec 0056 §3).

Данные тенанта (P-11, NG-7): что отель даёт бесплатно, какой службой, сколько
штук на гостя в день и что предложить сверх нормы. Только квоты, без языка
правил (NG-8). Поле `TenantConfig.service_quotas`; отдельный файл, а не ещё
один класс в `config.py`: тот за границей R-3.

Сколько уже заказано сегодня, считает `requests.api.check_quota`; что сказать
гостю — AI-инструмент заявки (PR 2 серии spec 0056).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from hospitality.platform.config_values import CATEGORY_KEY_PATTERN, LocalizedText

# Предел штук: и нормы на гостя в день, и одной заявки по квоте (CHECK колонки
# `service_requests.quota_units`, миграция 0049). Больше — опечатка, а не норма.
MAX_QUOTA_UNITS: Final = 20

# Цена штуки — целые тенге не дороже 1 000 000 ₸, как цена товара room service
# (`MINOR_UNITS_PER_TENGE`, `MAX_PRICE_MINOR` модуля; kernel его не импортирует,
# R-5 — расхождение ловит тест квот). `format_amount` показывает целые тенге:
# 50 050 тиын гость услышал бы как «500 ₸» (ревью PR #562).
QUOTA_PRICE_MINOR_PER_TENGE: Final = 100
MAX_QUOTA_UNIT_PRICE_MINOR: Final = 1_000_000 * QUOTA_PRICE_MINOR_PER_TENGE


class ServiceQuotaPaid(BaseModel):
    """Платная альтернатива сверх нормы (spec 0056 Р-6): служба и цена штуки.

    `unit_price_minor` — тиын за штуку; `None` — «сумму назовёт сотрудник»:
    цену, которой нет в конфиге, бот не называет (FOUNDATION §7.4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    category_key: str = Field(pattern=CATEGORY_KEY_PATTERN.pattern, max_length=63)
    unit_price_minor: int | None = Field(
        default=None,
        gt=0,
        le=MAX_QUOTA_UNIT_PRICE_MINOR,
        multiple_of=QUOTA_PRICE_MINOR_PER_TENGE,
    )


class ServiceQuota(BaseModel):
    """Норма бесплатного у отеля (spec 0056 Р-1, Р-2): предел — норма × гостей Stay.

    `key` — метка заявки по этой квоте (`service_requests.quota_key`) и значение
    enum'а инструмента; `names` — название в тексте гостю. Службы — ключи
    категорий заявок: что они у отеля есть, проверяет онбординг.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=CATEGORY_KEY_PATTERN.pattern, max_length=63)
    names: LocalizedText
    category_key: str = Field(pattern=CATEGORY_KEY_PATTERN.pattern, max_length=63)
    per_guest_per_day: int = Field(ge=1, le=MAX_QUOTA_UNITS)
    # Нет — сверх нормы бот отвечает «подскажут на ресепшене» (spec 0056 §5).
    paid: ServiceQuotaPaid | None = None


def ensure_unique_quota_keys(quotas: tuple[ServiceQuota, ...]) -> tuple[ServiceQuota, ...]:
    """Ключи квот уникальны: две квоты с одним ключом считали бы одни заявки дважды."""
    seen: set[str] = set()
    for quota in quotas:
        if quota.key in seen:
            raise ValueError(f"duplicate service quota key: {quota.key!r}")
        seen.add(quota.key)
    return quotas
