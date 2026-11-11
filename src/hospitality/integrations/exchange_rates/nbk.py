"""Адаптер официальных курсов Национального банка РК (spec 0084 Р-2, Р-3).

Источник — `get_rates.cfm?fdate=ДД.ММ.ГГГГ` (nationalbank.kz/ru/page/RSS, сверено
11.11.2026): XML `<rates>` с датой и `<item>` на валюту — `title` код ISO 4217,
`description` тенге за `quant` единиц. На дату, курс которой ещё не установлен, ответ
без `<item>` — тогда адаптер спрашивает предыдущие дни (`lookback_dates`).

Курс на саму дату окончателен — копия живёт, пока процесс жив (старше недели
выбрасывается). Курс предыдущего дня — 10 минут: свежий мог выйти. Сеть подвела —
поиск по дням обрывается сразу (гость ждёт), прежняя копия отдаётся, если есть, иначе
`ExchangeRatesUnavailableError` (ERR-CONCIERGE-002); после отказа — пауза
`FAILURE_COOLDOWN`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from xml.etree import ElementTree

import httpx

from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_concierge_source_call
from hospitality.shared.ports.exchange_rates import (
    ExchangeRatesUnavailableError,
    OfficialRate,
    OfficialRates,
    lookback_dates,
)

logger = get_logger(module=__name__)

RATES_URL: Final = "https://nationalbank.kz/rss/get_rates.cfm"
ERR_RATES_UNAVAILABLE: Final = "ERR-CONCIERGE-002"
BASE_CURRENCY: Final = "KZT"
EARLIER_DAY_TTL: Final = timedelta(minutes=10)
FAILURE_COOLDOWN: Final = timedelta(seconds=60)
_KEEP_DAYS: Final = timedelta(days=7)
# Ответа ждёт гость на своём ходу (spec 0084 Р-3).
_TIMEOUT_SECONDS: Final = 3.0
# Ответ на дату — около 14 КБ; больше полумегабайта — не тот ответ.
_MAX_RESPONSE_BYTES: Final = 512 * 1024


class _NotPublishedError(Exception):
    """На эту дату курс ещё не установлен — ответ без `<item>`."""


@dataclass(frozen=True)
class _CachedRates:
    rates: OfficialRates
    expires_at: datetime | None  # None — курс на саму дату, окончателен


class NbkExchangeRatesAdapter:
    backend = "nbk"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._cache: dict[date, _CachedRates] = {}
        self._lock = asyncio.Lock()
        self._paused_until: datetime | None = None

    async def official_rates(self, on: date) -> OfficialRates:
        async with self._lock:
            now = self._clock()
            cached = self._cache.get(on)
            if cached is not None and (cached.expires_at is None or now < cached.expires_at):
                return cached.rates
            if self._paused_until is not None and now < self._paused_until:
                return _cached_or_raise(cached, "paused after a failed request")
            try:
                rates = await self._latest(on)
            except ExchangeRatesUnavailableError as error:
                self._paused_until = now + FAILURE_COOLDOWN
                record_concierge_source_call("exchange_rates", "error")
                logger.warning(
                    "concierge_source_failed",
                    source="exchange_rates",
                    error_code=ERR_RATES_UNAVAILABLE,
                    reason=str(error),
                )
                return _cached_or_raise(cached, str(error))
            self._paused_until = None
            final = rates.effective_date == on
            self._cache[on] = _CachedRates(rates, None if final else now + EARLIER_DAY_TTL)
            self._forget_before(on - _KEEP_DAYS)
            return rates

    async def check(self) -> bool:
        try:
            params = {"fdate": _fdate(self._clock().date())}
            async with self._client() as client:
                response = await client.get(RATES_URL, params=params)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT_SECONDS, follow_redirects=False
        )

    async def _latest(self, on: date) -> OfficialRates:
        for effective_date in lookback_dates(on):
            try:
                return await self._fetch(effective_date)
            except _NotPublishedError:
                continue
        raise ExchangeRatesUnavailableError(f"no official rates in force on {on.isoformat()}")

    async def _fetch(self, effective_date: date) -> OfficialRates:
        params = {"fdate": _fdate(effective_date)}
        try:
            async with (
                self._client() as client,
                client.stream("GET", RATES_URL, params=params) as answer,
            ):
                body = await _read_capped(answer)
        except httpx.HTTPError as error:
            raise ExchangeRatesUnavailableError(f"no answer: {type(error).__name__}") from error
        if answer.status_code != 200:
            raise ExchangeRatesUnavailableError(f"status {answer.status_code}")
        try:
            rates = parse_rates(body, effective_date)
        except _NotPublishedError:
            # Банк ответил — запрос удался, курса на дату просто ещё нет. Ответ не по
            # формату сюда не попадает: он станет `error` в `official_rates`.
            record_concierge_source_call("exchange_rates", "ok")
            raise
        record_concierge_source_call("exchange_rates", "ok")
        return rates

    def _forget_before(self, oldest: date) -> None:
        for key in [key for key in self._cache if key < oldest]:
            del self._cache[key]


def parse_rates(body: bytes, effective_date: date) -> OfficialRates:
    """XML на дату → курсы; без `<item>` — `_NotPublishedError`, не тот формат — отказ.

    Строка одной валюты не по формату пропускается с WARNING: остальные курсы гостю
    ценнее, чем отказ из-за одной.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ExchangeRatesUnavailableError("answer is not XML") from error
    items = root.findall("item") if root.tag == "rates" else None
    if items is None:
        raise ExchangeRatesUnavailableError("answer does not match the rates format")
    if not items:
        raise _NotPublishedError
    if (root.findtext("date") or "").strip() != _fdate(effective_date):
        raise ExchangeRatesUnavailableError("answer is for another date")
    rates = [rate for rate in (_rate(item) for item in items) if rate is not None]
    try:
        return OfficialRates(BASE_CURRENCY, effective_date, tuple(rates))
    except ValueError as error:
        raise ExchangeRatesUnavailableError(f"answer is not a valid rates set: {error}") from error


def _rate(item: ElementTree.Element) -> OfficialRate | None:
    currency = (item.findtext("title") or "").strip()
    try:
        return OfficialRate(
            currency=currency,
            units=int((item.findtext("quant") or "").strip()),
            rate=Decimal((item.findtext("description") or "").strip()),
        )
    except (ValueError, InvalidOperation):
        logger.warning("exchange_rate_item_skipped", source="exchange_rates", currency=currency[:8])
        return None


def _fdate(day: date) -> str:
    return day.strftime("%d.%m.%Y")


async def _read_capped(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ExchangeRatesUnavailableError("answer is larger than expected")
    return bytes(body)


def _cached_or_raise(cached: _CachedRates | None, reason: str) -> OfficialRates:
    if cached is not None:
        logger.info("concierge_source_stale_copy", source="exchange_rates")
        return cached.rates
    raise ExchangeRatesUnavailableError(f"official rates are unavailable: {reason}")
