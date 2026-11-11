"""Контрактные тесты порта официальных курсов (R-7, ADR-007, spec 0084 §3, §5).

Канон — `tests/test_virus_scan_port.py`: один набор на оба адаптера параметризацией
фикстуры. Адаптер Национального банка говорит с `httpx.MockTransport`, который отвечает
XML `get_rates.cfm` из состояния Fake-адаптера: курс, установленный ровно на дату, —
строками `<item>`, иначе пустой `<rates>`, как у банка до публикации. Значит, оба
адаптера обязаны отдать одно и то же. Ниже — то, что есть только у банка: формат даты,
кэш, обрыв поиска по дням, пауза и строки не по формату. Формат сверен с живым
nationalbank.kz 11.11.2026.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from hospitality.integrations.exchange_rates import (
    FakeExchangeRatesAdapter,
    NbkExchangeRatesAdapter,
    build_exchange_rates,
)
from hospitality.integrations.exchange_rates.fake import DEFAULT_RATES
from hospitality.integrations.exchange_rates.nbk import (
    EARLIER_DAY_TTL,
    FAILURE_COOLDOWN,
    RATES_URL,
)
from hospitality.shared.config import Settings
from hospitality.shared.ports import exchange_rates
from hospitality.shared.ports.exchange_rates import (
    ExchangeRatesNotConfiguredError,
    ExchangeRatesPort,
    ExchangeRatesUnavailableError,
    OfficialRate,
    OfficialRates,
    get_exchange_rates,
    lookback_dates,
)

TODAY = date(2026, 11, 11)
START = datetime(2026, 11, 11, 6, 20, tzinfo=UTC)
RATES = (
    OfficialRate(currency="USD", units=1, rate=Decimal("447.52")),
    OfficialRate(currency="EUR", units=1, rate=Decimal("502.21")),
    OfficialRate(currency="KRW", units=100, rate=Decimal("33.44")),
    OfficialRate(currency="UZS", units=100, rate=Decimal("3.74")),
)
YESTERDAY_RATES = (OfficialRate(currency="USD", units=1, rate=Decimal("448.12")),)


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class NbkServer:
    """nationalbank.kz в памяти: курс на дату — у Fake, `mode` — как себя вести."""

    fake: FakeExchangeRatesAdapter
    mode: str = "normal"  # normal | status:<код> | timeout | garbage | huge
    timeout_on: date | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        day_text, month_text, year_text = request.url.params["fdate"].split(".")
        day = date(int(year_text), int(month_text), int(day_text))
        if self.mode == "timeout" or day == self.timeout_on:
            raise httpx.ConnectTimeout("silent bank", request=request)
        if self.mode.startswith("status:"):
            return httpx.Response(int(self.mode.split(":")[1]))
        if self.mode == "garbage":
            return httpx.Response(200, content=b"<html><body>maintenance</body>")
        if self.mode == "huge":
            return httpx.Response(200, content=b"<rates>" + b" " * (600 * 1024) + b"</rates>")
        try:
            found = await self.fake.official_rates(day)
        except ExchangeRatesUnavailableError:
            if self.fake.unavailable:
                return httpx.Response(503)
            return httpx.Response(200, content=render_rates(day, ()))
        published = found.rates if found.effective_date == day else ()
        return httpx.Response(200, content=render_rates(day, published))


def render_rates(day: date, rates: Sequence[OfficialRate], *, extra_items: str = "") -> bytes:
    """Ответ `get_rates.cfm` на дату, как его отдаёт банк."""
    items = "".join(
        "<item><fullname>ВАЛЮТА</fullname>"
        f"<title>{rate.currency}</title><description>{rate.rate}</description>"
        f"<quant>{rate.units}</quant><index>DOWN</index><change>-0.60</change></item>"
        for rate in rates
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<rates><generator>Alternate RSS Builder'
        "</generator><title>Official exchange rates of National Bank of Republic Kazakhstan"
        f"</title><date>{day:%d.%m.%Y}</date>{items}{extra_items}</rates>"
    ).encode()


@dataclass
class Harness:
    adapter: ExchangeRatesPort
    fake: FakeExchangeRatesAdapter


@pytest.fixture(params=["fake", "nbk"])
def harness(request: pytest.FixtureRequest) -> Harness:
    fake = FakeExchangeRatesAdapter()
    if request.param == "fake":
        return Harness(fake, fake)
    server = NbkServer(fake)
    adapter = NbkExchangeRatesAdapter(transport=httpx.MockTransport(server.handle), clock=Clock())
    return Harness(adapter, fake)


# --- контракт: оба адаптера ----------------------------------------------------


async def test_rates_set_for_the_day(harness: Harness) -> None:
    harness.fake.publish(TODAY, RATES)
    result = await harness.adapter.official_rates(TODAY)
    assert result == OfficialRates("KZT", TODAY, RATES)
    krw = result.find("KRW")
    assert krw is not None and (krw.units, krw.rate) == (100, Decimal("33.44"))
    assert result.find("GBP") is None


async def test_day_not_yet_set_gets_the_latest_earlier_rates(harness: Harness) -> None:
    harness.fake.publish(TODAY - timedelta(days=1), YESTERDAY_RATES)
    result = await harness.adapter.official_rates(TODAY)
    assert result.effective_date == TODAY - timedelta(days=1)
    assert result.rates == YESTERDAY_RATES


async def test_nothing_within_the_lookback_is_unavailable(harness: Harness) -> None:
    harness.fake.publish(TODAY - timedelta(days=4), YESTERDAY_RATES)
    with pytest.raises(ExchangeRatesUnavailableError):
        await harness.adapter.official_rates(TODAY)


async def test_unavailable_provider_raises(harness: Harness) -> None:
    harness.fake.unavailable = True
    with pytest.raises(ExchangeRatesUnavailableError):
        await harness.adapter.official_rates(TODAY)


async def test_check_reports_provider_health(harness: Harness) -> None:
    assert await harness.adapter.check() is True
    harness.fake.unavailable = True
    assert await harness.adapter.check() is False


async def test_nothing_published_gives_round_default_rates_any_day(harness: Harness) -> None:
    result = await harness.adapter.official_rates(TODAY)
    assert result == OfficialRates("KZT", TODAY, DEFAULT_RATES)


# --- Национальный банк: запрос, кэш, отказы ------------------------------------


def _nbk() -> tuple[NbkExchangeRatesAdapter, NbkServer, Clock]:
    clock = Clock()
    fake = FakeExchangeRatesAdapter()
    fake.publish(TODAY - timedelta(days=1), YESTERDAY_RATES)
    server = NbkServer(fake)
    adapter = NbkExchangeRatesAdapter(transport=httpx.MockTransport(server.handle), clock=clock)
    return adapter, server, clock


async def test_request_asks_the_bank_by_day_month_year() -> None:
    adapter, server, _clock = _nbk()
    server.fake.publish(TODAY, RATES)
    await adapter.official_rates(TODAY)
    (request,) = server.requests
    assert str(request.url).startswith(RATES_URL + "?")
    assert request.url.params["fdate"] == "11.11.2026"


async def test_rates_set_for_the_day_are_not_asked_again() -> None:
    adapter, server, clock = _nbk()
    server.fake.publish(TODAY, RATES)
    await adapter.official_rates(TODAY)
    clock.advance(timedelta(hours=20))
    assert (await adapter.official_rates(TODAY)).rates == RATES
    assert len(server.requests) == 1


async def test_earlier_day_rates_are_asked_again_after_ten_minutes() -> None:
    adapter, server, clock = _nbk()
    assert (await adapter.official_rates(TODAY)).effective_date == TODAY - timedelta(days=1)
    assert len(server.requests) == 2
    server.fake.publish(TODAY, RATES)
    clock.advance(EARLIER_DAY_TTL - timedelta(seconds=1))
    assert (await adapter.official_rates(TODAY)).rates == YESTERDAY_RATES
    assert len(server.requests) == 2
    clock.advance(timedelta(seconds=2))
    assert (await adapter.official_rates(TODAY)).rates == RATES


async def test_network_failure_stops_the_lookback_at_once() -> None:
    adapter, server, _clock = _nbk()
    server.timeout_on = TODAY - timedelta(days=1)
    with pytest.raises(ExchangeRatesUnavailableError, match="ConnectTimeout"):
        await adapter.official_rates(TODAY)
    assert len(server.requests) == 2


async def test_failure_pauses_the_bank_then_asks_again() -> None:
    adapter, server, clock = _nbk()
    server.mode = "status:502"
    with pytest.raises(ExchangeRatesUnavailableError):
        await adapter.official_rates(TODAY)
    server.mode = "normal"
    clock.advance(FAILURE_COOLDOWN - timedelta(seconds=1))
    with pytest.raises(ExchangeRatesUnavailableError, match="paused"):
        await adapter.official_rates(TODAY)
    assert len(server.requests) == 1
    clock.advance(timedelta(seconds=2))
    assert (await adapter.official_rates(TODAY)).rates == YESTERDAY_RATES


async def test_failure_serves_the_earlier_copy() -> None:
    adapter, server, clock = _nbk()
    copy = await adapter.official_rates(TODAY)
    server.mode = "timeout"
    clock.advance(EARLIER_DAY_TTL + timedelta(minutes=1))
    assert await adapter.official_rates(TODAY) == copy


@pytest.mark.parametrize("mode", ["garbage", "huge", "status:404"])
async def test_answer_off_format_is_unavailable(mode: str) -> None:
    adapter, server, _clock = _nbk()
    server.mode = mode
    with pytest.raises(ExchangeRatesUnavailableError):
        await adapter.official_rates(TODAY)


async def test_bad_item_is_skipped_the_rest_is_kept() -> None:
    broken = (
        "<item><title>usd</title><description>1</description><quant>1</quant></item>"
        "<item><title>EUR</title><description>n/a</description><quant>1</quant></item>"
        "<item><title>GBP</title><description>600.84</description><quant>0</quant></item>"
    )
    body = render_rates(TODAY, RATES[:1], extra_items=broken)
    adapter = NbkExchangeRatesAdapter(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
        clock=Clock(),
    )
    assert (await adapter.official_rates(TODAY)).rates == RATES[:1]


@pytest.mark.parametrize(
    "body",
    [
        render_rates(TODAY - timedelta(days=1), RATES),
        render_rates(TODAY, (), extra_items="<item><title>EUR</title></item>"),
        render_rates(TODAY, RATES[:1] * 2),
        b"<feed><item/></feed>",
    ],
    ids=["another-date", "only-bad-items", "duplicate-currency", "not-rates"],
)
async def test_answer_that_is_not_a_rates_set_is_unavailable(body: bytes) -> None:
    adapter = NbkExchangeRatesAdapter(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
        clock=Clock(),
    )
    with pytest.raises(ExchangeRatesUnavailableError):
        await adapter.official_rates(TODAY)


# --- порт: значения, выбор адаптера --------------------------------------------


def test_rate_values_check_themselves() -> None:
    for currency, units, rate in [
        ("usd", 1, "1"),
        ("USDT", 1, "1"),
        ("USD", 0, "1"),
        ("USD", 1, "0"),
        ("USD", 1, "NaN"),
    ]:
        with pytest.raises(ValueError):
            OfficialRate(currency=currency, units=units, rate=Decimal(rate))
    with pytest.raises(ValueError, match="at least one"):
        OfficialRates("KZT", TODAY, ())
    with pytest.raises(ValueError, match="once"):
        OfficialRates("KZT", TODAY, RATES[:1] * 2)


def test_lookback_covers_the_day_and_three_before_it() -> None:
    assert lookback_dates(TODAY) == [TODAY - timedelta(days=offset) for offset in range(4)]


def test_backend_setting_selects_adapter() -> None:
    fake = build_exchange_rates(Settings(exchange_rates_backend="fake"))
    assert isinstance(fake, FakeExchangeRatesAdapter)
    nbk = build_exchange_rates(Settings(exchange_rates_backend="nbk"))
    assert isinstance(nbk, NbkExchangeRatesAdapter)
    assert nbk.backend == "nbk"


def test_port_without_adapter_is_a_build_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exchange_rates, "_exchange_rates", None)
    with pytest.raises(ExchangeRatesNotConfiguredError):
        get_exchange_rates()
