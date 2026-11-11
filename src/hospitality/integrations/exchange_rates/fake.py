"""Fake-адаптер официальных курсов (ADR-007, spec 0084): курсы в памяти процесса.

Пока ничего не опубликовано, отдаёт `DEFAULT_RATES` на любой день — так локальный
uvicorn, CI и демо работают без сети; числа — круглые, чтобы их не приняли за
настоящий курс. После первого `publish` ведёт себя как Национальный банк: курс есть
только на опубликованные даты, на день без курса — последний из `lookback_dates`, а
дальше — `ExchangeRatesUnavailableError`. `unavailable = True` — провайдер лежит.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from hospitality.shared.ports.exchange_rates import (
    ExchangeRatesUnavailableError,
    OfficialRate,
    OfficialRates,
    lookback_dates,
)

BASE_CURRENCY: Final = "KZT"
DEFAULT_RATES: Final = (
    OfficialRate(currency="USD", units=1, rate=Decimal("500.00")),
    OfficialRate(currency="EUR", units=1, rate=Decimal("550.00")),
    OfficialRate(currency="RUB", units=1, rate=Decimal("5.00")),
    OfficialRate(currency="CNY", units=1, rate=Decimal("70.00")),
    OfficialRate(currency="KRW", units=100, rate=Decimal("35.00")),
)


class FakeExchangeRatesAdapter:
    backend = "fake"

    def __init__(self) -> None:
        self.unavailable = False
        self._published: dict[date, tuple[OfficialRate, ...]] = {}

    def publish(self, effective_date: date, rates: Sequence[OfficialRate]) -> None:
        """Курсы с этой даты — как их установил бы центральный банк."""
        self._published[effective_date] = tuple(rates)

    async def official_rates(self, on: date) -> OfficialRates:
        if self.unavailable:
            raise ExchangeRatesUnavailableError("fake exchange rates are marked unavailable")
        if not self._published:
            return OfficialRates(BASE_CURRENCY, on, DEFAULT_RATES)
        for effective_date in lookback_dates(on):
            rates = self._published.get(effective_date)
            if rates:
                return OfficialRates(BASE_CURRENCY, effective_date, rates)
        raise ExchangeRatesUnavailableError(f"no official rates in force on {on.isoformat()}")

    async def check(self) -> bool:
        return not self.unavailable
