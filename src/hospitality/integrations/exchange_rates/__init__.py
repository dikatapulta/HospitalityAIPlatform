"""Адаптеры порта официальных курсов валют (spec 0084) и выбор по настройкам."""

from __future__ import annotations

from hospitality.integrations.exchange_rates.fake import FakeExchangeRatesAdapter
from hospitality.integrations.exchange_rates.nbk import NbkExchangeRatesAdapter
from hospitality.shared.config import Settings
from hospitality.shared.ports.exchange_rates import ExchangeRatesPort

__all__ = ["FakeExchangeRatesAdapter", "NbkExchangeRatesAdapter", "build_exchange_rates"]


def build_exchange_rates(settings: Settings) -> ExchangeRatesPort:
    """Адаптер по `EXCHANGE_RATES_BACKEND` — зовёт только composition root."""
    if settings.exchange_rates_backend == "nbk":
        return NbkExchangeRatesAdapter()
    return FakeExchangeRatesAdapter()
