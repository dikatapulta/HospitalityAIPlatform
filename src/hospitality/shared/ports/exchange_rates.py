"""Порт официальных курсов валют консьержа (spec 0084, FOUNDATION §8, P-3).

Устроен по канону `shared/ports/file_storage.py`: `typing.Protocol` с
async-методами, исключения порта рядом, адаптеры — `integrations/exchange_rates/`
(Fake обязателен, ADR-007), общий контрактный набор
`tests/test_exchange_rates_port.py`, адаптер в порт кладёт только composition
root (`set_exchange_rates`).

Курс — справка для гостя, а не основа расчёта: суммы отеля по нему никто не
пересчитывает (spec 0084 Р-5). Поэтому число хранится так, как его опубликовал
центральный банк, — тенге за `units` единиц валюты, без деления и округления.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Protocol

# Сколько дней назад порт ищет курс, если на сам день его ещё нет (spec 0084 §3).
MAX_LOOKBACK_DAYS: Final = 3

_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


class ExchangeRatesUnavailableError(Exception):
    """Курса нет: нет соединения, таймаут, ответ не по формату или курс не опубликован."""


class ExchangeRatesNotConfiguredError(RuntimeError):
    """Composition root не положил адаптер — программная ошибка сборки процесса."""


@dataclass(frozen=True)
class OfficialRate:
    """`rate` единиц базовой валюты за `units` единиц `currency` — как опубликовано."""

    currency: str
    units: int
    rate: Decimal

    def __post_init__(self) -> None:
        if not _CURRENCY_CODE.fullmatch(self.currency):
            raise ValueError("currency must be an ISO 4217 code: three capital letters")
        if self.units < 1:
            raise ValueError("units must be positive")
        if not (self.rate.is_finite() and self.rate > 0):
            raise ValueError("rate must be a positive number")


@dataclass(frozen=True)
class OfficialRates:
    """Курсы центрального банка, действующие с `effective_date`."""

    base_currency: str
    effective_date: date
    rates: tuple[OfficialRate, ...]

    def __post_init__(self) -> None:
        if not _CURRENCY_CODE.fullmatch(self.base_currency):
            raise ValueError("base_currency must be an ISO 4217 code: three capital letters")
        if not self.rates:
            raise ValueError("official rates must have at least one currency")
        codes = [rate.currency for rate in self.rates]
        if len(set(codes)) != len(codes):
            raise ValueError("each currency must appear once")

    def find(self, currency: str) -> OfficialRate | None:
        """Курс валюты по коду; нет такой — None."""
        return next((rate for rate in self.rates if rate.currency == currency), None)


def lookback_dates(on: date) -> list[date]:
    """Даты, на которые порт ищет курс дня `on`: сам день и `MAX_LOOKBACK_DAYS` до него."""
    return [on - timedelta(days=offset) for offset in range(MAX_LOOKBACK_DAYS + 1)]


class ExchangeRatesPort(Protocol):
    """Официальные курсы валют к национальной валюте.

    Контракт (общие тесты — `tests/test_exchange_rates_port.py`):
    - `official_rates(on)` — курсы, действующие в день `on`: установленные на этот
      день, иначе последние из `lookback_dates(on)`; `effective_date` — их дата;
    - курса нет ни на одну из этих дат или провайдер не ответил —
      `ExchangeRatesUnavailableError`, а не выдуманный или пустой набор;
    - `check` — health адаптера (FOUNDATION §8 правило 1): `True`, если провайдер отвечает.
    """

    backend: str

    async def official_rates(self, on: date) -> OfficialRates: ...

    async def check(self) -> bool: ...


_exchange_rates: ExchangeRatesPort | None = None


def set_exchange_rates(exchange_rates: ExchangeRatesPort) -> None:
    """Положить адаптер процесса. Зовёт только composition root (и тесты)."""
    global _exchange_rates
    _exchange_rates = exchange_rates


def get_exchange_rates() -> ExchangeRatesPort:
    """Адаптер процесса; не положен — `ExchangeRatesNotConfiguredError`."""
    if _exchange_rates is None:
        raise ExchangeRatesNotConfiguredError(
            "exchange rates are not configured: composition root must call set_exchange_rates()"
        )
    return _exchange_rates


def exchange_rates_or_none() -> ExchangeRatesPort | None:
    """То же без исключения — тестам, которым нужно вернуть прежний адаптер."""
    return _exchange_rates
