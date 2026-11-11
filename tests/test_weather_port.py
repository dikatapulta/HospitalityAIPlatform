"""Контрактные тесты порта погоды (R-7, ADR-007, spec 0084 §3, §5).

Канон — `tests/test_virus_scan_port.py`: один набор на оба адаптера параметризацией
фикстуры. Адаптер MET Norway говорит с `httpx.MockTransport`, который отвечает форматом
Locationforecast compact из состояния Fake-адаптера, — значит, оба адаптера обязаны
отдать одинаковый прогноз. Ниже — то, что есть только у MET: условия провайдера
(User-Agent, 4 знака, `Expires`, `If-Modified-Since`), отказы, пауза и прежняя копия.
Формат ответа сверен с живым api.met.no 11.11.2026.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from hospitality.integrations.weather import (
    FakeWeatherAdapter,
    MetNorwayWeatherAdapter,
    build_weather,
    met_norway,
)
from hospitality.integrations.weather.met_norway import (
    FAILURE_COOLDOWN,
    FORECAST_URL,
    STALE_GRACE,
    condition_from_symbol,
)
from hospitality.shared.config import Settings
from hospitality.shared.ports import weather
from hospitality.shared.ports.weather import (
    GeoPoint,
    WeatherCondition,
    WeatherForecast,
    WeatherHour,
    WeatherNotConfiguredError,
    WeatherPort,
    WeatherUnavailableError,
    get_weather,
)

ALMATY = GeoPoint(43.2389, 76.8897)
START = datetime(2026, 11, 11, 6, 20, tzinfo=UTC)
_USER_AGENT = "test-suite/1.0 example.org"

# Обратная карта для тестового сервера: состояние неба → символ MET.
_SYMBOLS: dict[WeatherCondition, str] = {
    WeatherCondition.CLEAR: "clearsky_day",
    WeatherCondition.PARTLY_CLOUDY: "partlycloudy_night",
    WeatherCondition.CLOUDY: "cloudy",
    WeatherCondition.FOG: "fog",
    WeatherCondition.LIGHT_RAIN: "lightrainshowers_day",
    WeatherCondition.RAIN: "rain",
    WeatherCondition.HEAVY_RAIN: "heavyrain",
    WeatherCondition.SLEET: "sleet",
    WeatherCondition.LIGHT_SNOW: "lightsnow",
    WeatherCondition.SNOW: "snowshowers_polartwilight",
    WeatherCondition.HEAVY_SNOW: "heavysnow",
    WeatherCondition.THUNDERSTORM: "rainandthunder",
}


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class MetServer:
    """api.met.no в памяти: прогноз берёт у Fake, `mode` — как себя вести."""

    fake: FakeWeatherAdapter
    clock: Clock
    mode: str = "normal"  # normal | status:<код> | garbage | huge | timeout
    expires_in: timedelta = timedelta(minutes=30)
    last_modified: str = "Wed, 11 Nov 2026 05:55:00 GMT"
    requests: list[httpx.Request] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("silent provider", request=request)
        if self.mode.startswith("status:"):
            return httpx.Response(int(self.mode.split(":")[1]))
        if request.url.path.endswith("/status"):
            return httpx.Response(503 if self.fake.unavailable else 200, json={})
        if self.mode == "garbage":
            return httpx.Response(200, content=b"<html>maintenance</html>")
        if self.mode == "huge":
            return httpx.Response(200, content=b" " * (3 * 1024 * 1024))
        headers = {
            "Expires": format_datetime(self.clock.now + self.expires_in, usegmt=True),
            "Last-Modified": self.last_modified,
        }
        if request.headers.get("If-Modified-Since") == self.last_modified:
            return httpx.Response(304, headers=headers)
        try:
            forecast = await self.fake.forecast(_point(request))
        except WeatherUnavailableError:
            return httpx.Response(503)
        return httpx.Response(200, json=render_compact(forecast), headers=headers)


@dataclass
class Harness:
    adapter: WeatherPort
    fake: FakeWeatherAdapter
    clock: Clock


@pytest.fixture(params=["fake", "met_norway"])
def harness(request: pytest.FixtureRequest) -> Harness:
    clock = Clock()
    fake = FakeWeatherAdapter(clock=clock)
    if request.param == "fake":
        return Harness(fake, fake, clock)
    server = MetServer(fake, clock)
    adapter = MetNorwayWeatherAdapter(
        _USER_AGENT, transport=httpx.MockTransport(server.handle), clock=clock
    )
    return Harness(adapter, fake, clock)


def forecast_from(first: datetime, conditions: list[WeatherCondition | None]) -> WeatherForecast:
    hours = tuple(
        WeatherHour(
            starts_at=first + timedelta(hours=offset),
            air_temperature_c=-1.5 + offset,
            wind_speed_ms=2.4,
            condition=condition,
            precipitation_mm=0.3 if offset % 2 else None,
        )
        for offset, condition in enumerate(conditions)
    )
    return WeatherForecast(updated_at=first - timedelta(minutes=40), hours=hours)


def render_compact(forecast: WeatherForecast) -> dict[str, Any]:
    """Прогноз → тело ответа compact, как его отдаёт api.met.no."""
    series: list[dict[str, Any]] = []
    for hour in forecast.hours:
        symbol = "notasymbol_day" if hour.condition is None else _SYMBOLS[hour.condition]
        precipitation = (
            {} if hour.precipitation_mm is None else {"precipitation_amount": hour.precipitation_mm}
        )
        next_hour = {"summary": {"symbol_code": symbol}, "details": precipitation}
        series.append(
            {
                "time": _iso(hour.starts_at),
                "data": {
                    "instant": {
                        "details": {
                            "air_temperature": hour.air_temperature_c,
                            "wind_speed": hour.wind_speed_ms,
                            "relative_humidity": 80.0,
                        }
                    },
                    "next_1_hours": next_hour,
                    "next_6_hours": {"summary": {"symbol_code": "cloudy"}, "details": {}},
                },
            }
        )
    # Дальше часового отрезка MET идёт шагом 6 часов — без `next_1_hours`.
    series.append(
        {
            "time": _iso(forecast.hours[-1].starts_at + timedelta(hours=6)),
            "data": {
                "instant": {"details": {"air_temperature": 0.0, "wind_speed": 1.0}},
                "next_6_hours": {"summary": {"symbol_code": "snow"}, "details": {}},
            },
        }
    )
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [76.8897, 43.2389, 819]},
        "properties": {
            "meta": {"updated_at": _iso(forecast.updated_at), "units": {}},
            "timeseries": series,
        },
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _point(request: httpx.Request) -> GeoPoint:
    return GeoPoint(float(request.url.params["lat"]), float(request.url.params["lon"]))


def _current_hour() -> datetime:
    return START.replace(minute=0)


# --- контракт: оба адаптера ----------------------------------------------------


async def test_forecast_returns_the_points_hours(harness: Harness) -> None:
    expected = forecast_from(_current_hour(), list(WeatherCondition))
    harness.fake.set_forecast(ALMATY, expected)
    assert await harness.adapter.forecast(ALMATY) == expected


async def test_finished_hours_are_dropped(harness: Harness) -> None:
    stored = forecast_from(_current_hour() - timedelta(hours=3), [WeatherCondition.CLEAR] * 6)
    harness.fake.set_forecast(ALMATY, stored)
    result = await harness.adapter.forecast(ALMATY)
    assert [hour.starts_at for hour in result.hours] == [
        _current_hour(),
        _current_hour() + timedelta(hours=1),
        _current_hour() + timedelta(hours=2),
    ]


async def test_unknown_condition_is_none_not_a_guess(harness: Harness) -> None:
    harness.fake.set_forecast(ALMATY, forecast_from(_current_hour(), [None]))
    (hour,) = (await harness.adapter.forecast(ALMATY)).hours
    assert hour.condition is None


async def test_forecast_with_every_hour_finished_is_unavailable(harness: Harness) -> None:
    old = forecast_from(_current_hour() - timedelta(hours=5), [WeatherCondition.FOG] * 2)
    harness.fake.set_forecast(ALMATY, old)
    with pytest.raises(WeatherUnavailableError):
        await harness.adapter.forecast(ALMATY)


async def test_unavailable_provider_raises(harness: Harness) -> None:
    harness.fake.unavailable = True
    with pytest.raises(WeatherUnavailableError):
        await harness.adapter.forecast(ALMATY)


async def test_check_reports_provider_health(harness: Harness) -> None:
    assert await harness.adapter.check() is True
    harness.fake.unavailable = True
    assert await harness.adapter.check() is False


async def test_point_without_a_stored_forecast_gets_a_plausible_one(harness: Harness) -> None:
    result = await harness.adapter.forecast(GeoPoint(51.1694, 71.4491))
    assert len(result.hours) == 48
    assert result.hours[0].starts_at == _current_hour()
    assert {hour.condition for hour in result.hours} == {WeatherCondition.PARTLY_CLOUDY}
    assert all(1 <= hour.air_temperature_c <= 9 for hour in result.hours)


# --- MET Norway: условия провайдера --------------------------------------------


def _met(mode: str = "normal") -> tuple[MetNorwayWeatherAdapter, MetServer]:
    clock = Clock()
    fake = FakeWeatherAdapter(clock=clock)
    fake.set_forecast(ALMATY, forecast_from(_current_hour(), [WeatherCondition.RAIN] * 30))
    server = MetServer(fake, clock, mode=mode)
    adapter = MetNorwayWeatherAdapter(
        _USER_AGENT, transport=httpx.MockTransport(server.handle), clock=clock
    )
    return adapter, server


async def test_request_identifies_the_app_and_sends_four_decimals() -> None:
    adapter, server = _met()
    server.fake.set_forecast(
        GeoPoint(43.2389, 76.8898), forecast_from(_current_hour(), [WeatherCondition.CLEAR])
    )
    await adapter.forecast(GeoPoint(43.238912345, 76.889765))
    (request,) = server.requests
    assert str(request.url).startswith(FORECAST_URL + "?")
    assert request.url.params["lat"] == "43.2389"
    assert request.url.params["lon"] == "76.8898"
    assert request.headers["User-Agent"] == _USER_AGENT


async def test_no_repeat_before_expires_then_if_modified_since() -> None:
    adapter, server = _met()
    first = await adapter.forecast(ALMATY)
    server.clock.advance(timedelta(minutes=29))
    assert await adapter.forecast(ALMATY) == first
    assert len(server.requests) == 1
    server.clock.advance(timedelta(minutes=2))
    await adapter.forecast(ALMATY)
    assert len(server.requests) == 2
    assert server.requests[1].headers["If-Modified-Since"] == server.last_modified


async def test_not_modified_keeps_the_copy_and_renews_it() -> None:
    adapter, server = _met()
    await adapter.forecast(ALMATY)
    server.clock.advance(timedelta(minutes=31))
    server.fake.set_forecast(ALMATY, forecast_from(_current_hour(), [WeatherCondition.CLEAR]))
    renewed = await adapter.forecast(ALMATY)
    assert renewed.hours[0].condition is WeatherCondition.RAIN
    server.clock.advance(timedelta(minutes=20))
    await adapter.forecast(ALMATY)
    assert len(server.requests) == 2


@pytest.mark.parametrize("mode", ["status:429", "status:403", "status:500", "timeout", "garbage"])
async def test_failure_without_a_copy_is_unavailable(mode: str) -> None:
    adapter, _server = _met(mode)
    with pytest.raises(WeatherUnavailableError):
        await adapter.forecast(ALMATY)


async def test_failure_pauses_the_provider_for_everyone() -> None:
    adapter, server = _met("status:429")
    with pytest.raises(WeatherUnavailableError):
        await adapter.forecast(ALMATY)
    server.mode = "normal"
    server.clock.advance(FAILURE_COOLDOWN - timedelta(seconds=1))
    with pytest.raises(WeatherUnavailableError, match="paused"):
        await adapter.forecast(GeoPoint(51.1694, 71.4491))
    assert len(server.requests) == 1
    server.clock.advance(timedelta(seconds=2))
    assert (await adapter.forecast(ALMATY)).hours
    assert len(server.requests) == 2


async def test_failure_serves_a_young_copy_and_drops_an_old_one() -> None:
    adapter, server = _met()
    copy = await adapter.forecast(ALMATY)
    server.mode = "status:503"
    server.clock.advance(STALE_GRACE - timedelta(minutes=1))
    stale = await adapter.forecast(ALMATY)
    assert stale.hours[0].starts_at == copy.hours[3].starts_at
    server.clock.advance(FAILURE_COOLDOWN + timedelta(minutes=2))
    with pytest.raises(WeatherUnavailableError):
        await adapter.forecast(ALMATY)


async def test_deprecated_version_is_still_parsed() -> None:
    _adapter, server = _met()

    async def deprecated(request: httpx.Request) -> httpx.Response:
        answer = await server.handle(request)
        return httpx.Response(203, content=answer.content)

    adapter = MetNorwayWeatherAdapter(
        _USER_AGENT, transport=httpx.MockTransport(deprecated), clock=server.clock
    )
    assert (await adapter.forecast(ALMATY)).hours[0].condition is WeatherCondition.RAIN


async def test_oversized_answer_is_unavailable() -> None:
    adapter, _server = _met("huge")
    with pytest.raises(WeatherUnavailableError, match="larger"):
        await adapter.forecast(ALMATY)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.pop("properties"),
        lambda body: body["properties"]["timeseries"][0]["data"]["instant"]["details"].update(
            air_temperature=True
        ),
        lambda body: body["properties"]["timeseries"][0].update(time="2026-11-11T06:00:00"),
        lambda body: body["properties"]["timeseries"].reverse(),
        lambda body: body["properties"]["timeseries"][0]["data"].update(next_1_hours=None),
        lambda body: body["properties"]["timeseries"][0]["data"]["next_1_hours"].update(
            summary=None
        ),
        lambda body: body["properties"]["timeseries"][0]["data"]["next_1_hours"].update(
            details=None
        ),
    ],
    ids=[
        "no-properties",
        "bool-temperature",
        "naive-time",
        "unordered",
        "null-next-hour",
        "null-summary",
        "null-details",
    ],
)
async def test_answer_off_format_is_unavailable(mutate: Callable[[dict[str, Any]], Any]) -> None:
    clock = Clock()
    body = render_compact(forecast_from(_current_hour(), [WeatherCondition.CLEAR] * 3))
    mutate(body)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=json.dumps(body)))
    adapter = MetNorwayWeatherAdapter(_USER_AGENT, transport=transport, clock=clock)
    with pytest.raises(WeatherUnavailableError):
        await adapter.forecast(ALMATY)


async def test_slowly_dripping_answer_is_cut_by_the_call_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таймаут httpx — на фазу, а гость ждёт вызов целиком (ревью PR #1017): капающий
    ответ обрывает общий предел вызова, и провайдер встаёт на паузу, как при отказе."""
    monkeypatch.setattr(met_norway, "_TIMEOUT_SECONDS", 0.05)

    async def drip() -> AsyncIterator[bytes]:
        for _ in range(100):
            await asyncio.sleep(0.01)
            yield b" "

    requests: list[httpx.Request] = []

    def slow(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=drip())

    adapter = MetNorwayWeatherAdapter(_USER_AGENT, transport=httpx.MockTransport(slow))
    with pytest.raises(WeatherUnavailableError, match="TimeoutError"):
        await adapter.forecast(ALMATY)
    with pytest.raises(WeatherUnavailableError, match="paused"):
        await adapter.forecast(ALMATY)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("symbol", "condition"),
    [
        ("clearsky_night", WeatherCondition.CLEAR),
        ("fair_day", WeatherCondition.PARTLY_CLOUDY),
        ("lightrainshowers_day", WeatherCondition.LIGHT_RAIN),
        ("heavyrainshowers_night", WeatherCondition.HEAVY_RAIN),
        ("sleetshowers_day", WeatherCondition.SLEET),
        ("lightssnowshowersandthunder_day", WeatherCondition.THUNDERSTORM),
        ("heavysnow", WeatherCondition.HEAVY_SNOW),
        ("snow", WeatherCondition.SNOW),
        ("sandstorm", None),
        (42, None),
    ],
)
def test_met_symbols_map_to_conditions(symbol: object, condition: WeatherCondition | None) -> None:
    assert condition_from_symbol(symbol) is condition


def test_met_adapter_requires_a_user_agent() -> None:
    with pytest.raises(ValueError, match="User-Agent"):
        MetNorwayWeatherAdapter("  ")


# --- порт: значения, выбор адаптера --------------------------------------------


def test_geo_point_rounds_and_rejects_out_of_range() -> None:
    assert GeoPoint(43.238949, -76.88975) == GeoPoint(43.2389, -76.8898)
    for latitude, longitude in [(91.0, 0.0), (0.0, -180.5), (float("nan"), 0.0)]:
        with pytest.raises(ValueError):
            GeoPoint(latitude, longitude)


def test_weather_values_check_themselves() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        naive = START.replace(tzinfo=None)
        WeatherHour(starts_at=naive, air_temperature_c=1, wind_speed_ms=1)
    with pytest.raises(ValueError, match="wind_speed_ms"):
        WeatherHour(starts_at=START, air_temperature_c=1, wind_speed_ms=-1)
    with pytest.raises(ValueError, match="at least one"):
        WeatherForecast(updated_at=START, hours=())


def test_backend_setting_selects_adapter() -> None:
    assert isinstance(build_weather(Settings(weather_backend="fake")), FakeWeatherAdapter)
    met = build_weather(Settings(weather_backend="met_norway"))
    assert isinstance(met, MetNorwayWeatherAdapter)
    assert met.backend == "met_norway"


def test_port_without_adapter_is_a_build_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "_weather", None)
    with pytest.raises(WeatherNotConfiguredError):
        get_weather()
