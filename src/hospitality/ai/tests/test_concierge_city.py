"""Блок «Weather and exchange rates» (spec 0084 §4, §5 PR 2).

Порты здесь — заглушки теста: адаптеры из `ai/` не импортируются (контракт 2
import-linter); сами адаптеры проверяют `tests/test_weather_port.py` и
`tests/test_exchange_rates_port.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from hospitality.ai import concierge_city, orchestrator
from hospitality.ai.concierge_city import concierge_city_block, render_concierge_city_block
from hospitality.ai.evals import concierge_city as evals
from hospitality.ai.evals.bakeoff import _eval_tenant_id
from hospitality.ai.gateway.api import MockLlmProvider, MockTurn, ScriptedLlmProvider
from hospitality.ai.tools.base import ActiveRequest
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.ports import exchange_rates, weather
from hospitality.shared.ports.exchange_rates import (
    ExchangeRatesUnavailableError,
    OfficialRate,
    OfficialRates,
)
from hospitality.shared.ports.weather import (
    GeoPoint,
    WeatherCondition,
    WeatherForecast,
    WeatherHour,
    WeatherUnavailableError,
)
from hospitality.shared.tenancy import tenant_context

ALMATY = ZoneInfo("Asia/Almaty")
# 01:20 UTC — 06:20 в Алматы (UTC+5), среда 11 ноября.
NOW = datetime(2026, 11, 11, 1, 20, tzinfo=UTC)
CITY = {"name": "Almaty", "latitude": 43.2389, "longitude": 76.8897}
RATES = OfficialRates(
    "KZT",
    date(2026, 11, 11),
    (
        OfficialRate("KRW", 100, Decimal("33.44")),
        OfficialRate("USD", 1, Decimal("447.52")),
        OfficialRate("XDR", 1, Decimal("610.10")),
        OfficialRate("EUR", 1, Decimal("502.21")),
    ),
)


def _forecast(
    start: datetime, hours: int, *, updated_at: datetime | None = None
) -> WeatherForecast:
    """Часы с `start` подряд: температура — номер часа, на третьем часу — небо незнакомо."""
    return WeatherForecast(
        updated_at=updated_at or start,
        hours=tuple(
            WeatherHour(
                starts_at=start + timedelta(hours=offset),
                air_temperature_c=offset - 0.4,
                wind_speed_ms=2.4,
                condition=None if offset == 3 else WeatherCondition.LIGHT_RAIN,
            )
            for offset in range(hours)
        ),
    )


def _render(forecast: WeatherForecast | None, rates: OfficialRates | None = RATES) -> str:
    return render_concierge_city_block("Almaty", forecast, rates, now=NOW, zone=ALMATY)


def test_block_reads_hours_and_days_in_hotel_zone() -> None:
    block = _render(_forecast(datetime(2026, 11, 11, 1, tzinfo=UTC), 48))

    assert block.startswith("\n\n# Weather and exchange rates\n\n")
    assert (
        "Weather forecast for Almaty by MET Norway, hotel local time (updated 06:00):\n"
        "- now: 0 °C, light rain, wind 2 m/s\n"
        "- Wednesday 11 November: 09:00 3 °C · 12:00 6 °C light rain" in block
    )
    # Сутки — по поясу отеля: 00:00 четверга в Алматы — 19:00 UTC среды.
    assert "\n- Thursday 12 November: 00:00 18 °C light rain · 03:00 21 °C light rain" in block
    assert "21:00 39 °C light rain\n\n" in block
    assert "Friday" not in block


def test_rates_in_guest_currency_order_as_published() -> None:
    block = _render(_forecast(datetime(2026, 11, 11, 1, tzinfo=UTC), 3))

    assert (
        "Official exchange rates of the National Bank of Kazakhstan for 11 November 2026, "
        "in tenge (KZT):\n1 USD = 447.52 · 1 EUR = 502.21 · 100 KRW = 33.44\n\n" in block
    )
    assert "XDR" not in block


def test_rules_keep_the_bot_from_volunteering_and_converting() -> None:
    block = _render(None, None)

    assert "Do not bring it up yourself." in block
    assert "say once, briefly, that it is a MET Norway forecast" in block
    assert "exchange offices and the hotel's front desk have their own rates" in block
    assert "Never convert the hotel's prices or the guest's bill with these rates" in block


def test_missing_sources_say_so_instead_of_numbers() -> None:
    block = _render(None, None)

    assert (
        "Weather forecast for Almaty is not available right now: say so honestly, do not guess."
        in block
    )
    assert (
        "Official exchange rates are not available right now: say so honestly, do not guess."
        in block
    )
    assert "°C" not in block
    only_other_currencies = OfficialRates("KZT", date(2026, 11, 11), (RATES.rates[2],))
    assert "Official exchange rates are not available" in _render(None, only_other_currencies)


def test_forecast_of_yesterday_and_late_evening() -> None:
    """Прогноз посчитан вчера по поясу отеля — с датой; в 22:20 строки «сегодня» нет."""
    late = NOW + timedelta(hours=16)  # 22:20 в Алматы
    forecast = _forecast(
        datetime(2026, 11, 11, 17, tzinfo=UTC),
        12,
        updated_at=datetime(2026, 11, 10, 18, 30, tzinfo=UTC),
    )

    block = render_concierge_city_block("Almaty", forecast, RATES, now=late, zone=ALMATY)

    assert "(updated 10 November 23:30):\n- now: " in block
    assert "Wednesday" not in block
    assert "- Thursday 12 November: 00:00 " in block


class _Weather:
    backend = "test"

    def __init__(self, result: WeatherForecast | Exception | None = None) -> None:
        self.result = result
        self.points: list[GeoPoint] = []

    async def forecast(self, point: GeoPoint) -> WeatherForecast:
        self.points.append(point)
        if self.result is None:
            await asyncio.sleep(10)
        if isinstance(self.result, Exception):
            raise self.result
        assert self.result is not None
        return self.result

    async def check(self) -> bool:
        return True


class _Rates:
    backend = "test"

    def __init__(self, result: OfficialRates | Exception) -> None:
        self.result = result
        self.days: list[date] = []

    async def official_rates(self, on: date) -> OfficialRates:
        self.days.append(on)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def check(self) -> bool:
        return True


@pytest.fixture
def concierge_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ENABLE_CONCIERGE_CITY", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def ports(monkeypatch: pytest.MonkeyPatch) -> tuple[_Weather, _Rates]:
    stub_weather = _Weather(_forecast(NOW.replace(minute=0), 48))
    stub_rates = _Rates(RATES)
    monkeypatch.setattr(weather, "_weather", stub_weather)
    monkeypatch.setattr(exchange_rates, "_exchange_rates", stub_rates)
    return stub_weather, stub_rates


def _config(**changes: object) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile": {"city": "Almaty", "country_code": "KZ"},
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "concierge_city": CITY,
            **changes,
        }
    )


async def test_no_flag_no_config_or_no_city_means_no_block(
    ports: tuple[_Weather, _Rates], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_CONCIERGE_CITY", "false")
    get_settings.cache_clear()
    try:
        assert await concierge_city_block(_config(), now=NOW) == ""
    finally:
        get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_CONCIERGE_CITY", "true")
    get_settings.cache_clear()
    try:
        assert await concierge_city_block(None, now=NOW) == ""
        assert await concierge_city_block(_config(concierge_city=None), now=NOW) == ""
    finally:
        get_settings.cache_clear()
    assert ports[0].points == []


async def test_block_asks_the_hotel_point_and_its_local_day(
    concierge_enabled: None, ports: tuple[_Weather, _Rates]
) -> None:
    # 20:30 UTC 11 ноября — уже 12 ноября в Алматы.
    evening = datetime(2026, 11, 11, 20, 30, tzinfo=UTC)
    ports[0].result = _forecast(evening.replace(minute=0), 30)

    block = await concierge_city_block(_config(), now=evening)

    assert ports[0].points == [GeoPoint(43.2389, 76.8897)]
    assert ports[1].days == [date(2026, 11, 12)]
    assert "Weather forecast for Almaty by MET Norway" in block


@pytest.mark.parametrize(
    "failure",
    [
        WeatherUnavailableError("down"),
        ExchangeRatesUnavailableError("down"),
        AttributeError("'NoneType' object has no attribute 'get'"),
        weather.WeatherNotConfiguredError("no adapter"),
    ],
)
async def test_failed_source_becomes_a_not_available_line(
    concierge_enabled: None, ports: tuple[_Weather, _Rates], failure: Exception
) -> None:
    ports[0].result = failure
    ports[1].result = failure

    block = await concierge_city_block(_config(), now=NOW)

    assert "Weather forecast for Almaty is not available right now" in block
    assert "Official exchange rates are not available right now" in block


async def test_hanging_source_is_cut_by_the_deadline(
    concierge_enabled: None, ports: tuple[_Weather, _Rates], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Источник висит — ход ждёт не дольше предела, второй источник при этом отвечает."""
    monkeypatch.setattr(concierge_city, "SOURCE_DEADLINE_SECONDS", 0.05)
    ports[0].result = None

    block = await asyncio.wait_for(concierge_city_block(_config(), now=NOW), timeout=1)

    assert "Weather forecast for Almaty is not available right now" in block
    assert "1 USD = 447.52" in block


async def _configure(tenant_id: uuid.UUID) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, _config())


async def test_block_sits_among_turn_blocks_before_requests(
    demo_tenant: uuid.UUID, concierge_enabled: None, ports: tuple[_Weather, _Rates]
) -> None:
    await _configure(demo_tenant)
    request = ActiveRequest(
        id=uuid.uuid4(), status=requests_api.RequestStatus.NEW, summary="полотенца"
    )
    provider = MockLlmProvider(text="ответ")

    with tenant_context(demo_tenant):
        await orchestrator.handle_message(
            message="Какая завтра погода?",
            provider=provider,
            verified_room_number="305",
            active_requests=(request,),
        )

    system = provider.calls[0].system or ""
    assert (
        system.index("# Guest's verified room")
        < system.index("# Weather and exchange rates")
        < system.index("# Active service requests in this conversation")
    )


async def test_sources_down_the_turn_still_goes(
    demo_tenant: uuid.UUID, concierge_enabled: None, ports: tuple[_Weather, _Rates]
) -> None:
    """FOUNDATION §8 правило 2: отказ внешнего источника не ломает диалог гостя."""
    await _configure(demo_tenant)
    ports[0].result = WeatherUnavailableError("down")
    ports[1].result = RuntimeError("bug in an adapter")
    provider = MockLlmProvider(text="Сейчас не могу узнать погоду.")

    with tenant_context(demo_tenant):
        turn = await orchestrator.handle_message(message="Какая погода?", provider=provider)

    assert turn.reply_text == "Сейчас не могу узнать погоду."
    assert "Weather forecast for Almaty is not available" in (provider.calls[0].system or "")


async def test_flag_off_keeps_the_prompt_without_the_block(
    demo_tenant: uuid.UUID, ports: tuple[_Weather, _Rates]
) -> None:
    await _configure(demo_tenant)
    provider = MockLlmProvider(text="ответ")

    with tenant_context(demo_tenant):
        await orchestrator.handle_message(message="Какая погода?", provider=provider)

    assert "# Weather and exchange rates" not in (provider.calls[0].system or "")
    assert ports[0].points == []


async def test_concierge_evals_plumbing_on_scripted_provider(
    canonical_database: None, concierge_enabled: None, ports: tuple[_Weather, _Rates]
) -> None:
    """Обвязка платных evals проверяется бесплатно: город у eval-тенанта, блок с курсом
    в промпте и оценка хода по сценарию."""
    await evals._ensure_concierge_tenant()
    weather_turn = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("en", "weather"))
    unasked = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("ru", "unasked"))
    provider = ScriptedLlmProvider(
        [
            MockTurn(text="Tomorrow: about 2 °C and cloudy (forecast by MET Norway)."),
            MockTurn(text="Завтрак с 07:00. Кстати, сейчас 2 °C."),
        ]
    )
    with tenant_context(await _eval_tenant_id()):
        turns = [
            await orchestrator.handle_message(message=scenario.message, provider=provider)
            for scenario in (weather_turn, unasked)
        ]

    assert "1 USD = 447.52 · 1 EUR = 502.21 · 1 RUB = 5.41" in (provider.calls[0].system or "")
    assert len(evals.SCENARIOS) == 24
    assert evals._check(weather_turn, turns[0])[0] is True
    assert evals._check(unasked, turns[1])[0] is False
    bill = next(s for s in evals.SCENARIOS if (s.language, s.kind) == ("tr", "bill"))
    converted = orchestrator.OrchestratorTurn(
        kind=orchestrator.TurnKind.REPLY, reply_text="Yaklaşık 190 dolar eder."
    )
    assert evals._check(bill, converted)[0] is False
