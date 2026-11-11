"""Адаптер погоды MET Norway — Locationforecast 2.0 compact (spec 0084 Р-2, Р-3).

Условия провайдера (api.met.no/doc/TermsOfService, сверены 11.11.2026) — в README
рядом; здесь они кодом: опознающий User-Agent, координаты — 4 знака (их округляет
`GeoPoint`), повтор не раньше `Expires`, повтор — с `If-Modified-Since`, после отказа —
пауза `FAILURE_COOLDOWN` для всех точек. Не ответил — прежняя копия, пока она
моложе `STALE_GRACE`, иначе `WeatherUnavailableError` (ERR-CONCIERGE-001).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx

from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_concierge_source_call
from hospitality.shared.ports.weather import (
    COORDINATE_DECIMALS,
    GeoPoint,
    WeatherCondition,
    WeatherForecast,
    WeatherHour,
    WeatherUnavailableError,
    without_finished_hours,
)

logger = get_logger(module=__name__)

FORECAST_URL: Final = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
STATUS_URL: Final = "https://api.met.no/weatherapi/locationforecast/2.0/status"
ERR_WEATHER_UNAVAILABLE: Final = "ERR-CONCIERGE-001"
STALE_GRACE: Final = timedelta(hours=3)
FAILURE_COOLDOWN: Final = timedelta(seconds=60)
# Ответа ждёт гость на своём ходу (spec 0084 Р-3).
_TIMEOUT_SECONDS: Final = 3.0
# Ответ compact — около 40 КБ; больше двух мегабайт — не тот ответ.
_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
# Без `Expires` — полчаса, как обычно отвечает MET; рамки — против часов провайдера.
_DEFAULT_TTL: Final = timedelta(minutes=30)
_MIN_TTL: Final = timedelta(minutes=1)
_MAX_TTL: Final = timedelta(hours=6)

# Символ MET без суффикса времени суток (`_day`, `_night`, `_polartwilight`).
_PLAIN_SYMBOLS: Final[dict[str, WeatherCondition]] = {
    "clearsky": WeatherCondition.CLEAR,
    "fair": WeatherCondition.PARTLY_CLOUDY,
    "partlycloudy": WeatherCondition.PARTLY_CLOUDY,
    "cloudy": WeatherCondition.CLOUDY,
    "fog": WeatherCondition.FOG,
}


@dataclass(frozen=True)
class _CachedForecast:
    forecast: WeatherForecast
    confirmed_at: datetime
    expires_at: datetime
    last_modified: str | None


class MetNorwayWeatherAdapter:
    backend = "met_norway"

    def __init__(
        self,
        user_agent: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("MET Norway requires an identifying User-Agent")
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._transport = transport
        self._clock = clock
        self._cache: dict[GeoPoint, _CachedForecast] = {}
        self._locks: dict[GeoPoint, asyncio.Lock] = {}
        self._paused_until: datetime | None = None

    async def forecast(self, point: GeoPoint) -> WeatherForecast:
        # Соседние ходы ждут один запрос, а не шлют каждый свой (до 20 в секунду на всех).
        async with self._locks.setdefault(point, asyncio.Lock()):
            now = self._clock()
            cached = self._cache.get(point)
            if cached is not None and now < cached.expires_at:
                return _upcoming(cached.forecast, now)
            if self._paused_until is not None and now < self._paused_until:
                return _stale_or_raise(cached, now, "paused after a failed request")
            try:
                fresh = await self._fetch(point, cached)
            except WeatherUnavailableError as error:
                self._paused_until = now + FAILURE_COOLDOWN
                record_concierge_source_call("weather", "error")
                logger.warning(
                    "concierge_source_failed",
                    source="weather",
                    error_code=ERR_WEATHER_UNAVAILABLE,
                    reason=str(error),
                )
                return _stale_or_raise(cached, now, str(error))
            self._paused_until = None
            self._cache[point] = fresh
            return _upcoming(fresh.forecast, now)

    async def check(self) -> bool:
        try:
            async with self._client() as client:
                response = await client.get(STATUS_URL, headers=self._headers)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT_SECONDS, follow_redirects=False
        )

    async def _fetch(self, point: GeoPoint, cached: _CachedForecast | None) -> _CachedForecast:
        headers = dict(self._headers)
        if cached is not None and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        params = {"lat": _coordinate(point.latitude), "lon": _coordinate(point.longitude)}
        try:
            # Таймаут httpx — на каждую фазу (соединение, кусок чтения), а гость ждёт
            # вызов целиком: медленно капающий ответ режет общий предел.
            async with (
                asyncio.timeout(_TIMEOUT_SECONDS),
                self._client() as client,
                client.stream("GET", FORECAST_URL, params=params, headers=headers) as response,
            ):
                body = await _read_capped(response)
        except (httpx.HTTPError, TimeoutError) as error:
            raise WeatherUnavailableError(f"no answer: {type(error).__name__}") from error
        now = self._clock()
        expires_at = _expires_at(response.headers.get("Expires"), now)
        if response.status_code == 304 and cached is not None:
            record_concierge_source_call("weather", "not_modified")
            return replace(cached, confirmed_at=now, expires_at=expires_at)
        if response.status_code == 203:
            # Версия продукта объявлена устаревшей: обычно её выключают через месяц.
            logger.warning("weather_provider_version_deprecated", source="weather")
        elif response.status_code != 200:
            raise WeatherUnavailableError(f"status {response.status_code}")
        forecast = parse_compact(body)
        record_concierge_source_call("weather", "ok")
        return _CachedForecast(forecast, now, expires_at, response.headers.get("Last-Modified"))


def parse_compact(body: bytes) -> WeatherForecast:
    """Ответ compact → прогноз; не тот формат — `WeatherUnavailableError`.

    Берутся только часы с `next_1_hours`: дальше MET переходит на шаг 6 часов.
    """
    try:
        properties = json.loads(body)["properties"]
        updated_at = datetime.fromisoformat(properties["meta"]["updated_at"])
        hours = tuple(
            _hour(entry) for entry in properties["timeseries"] if "next_1_hours" in entry["data"]
        )
        return WeatherForecast(updated_at=updated_at, hours=hours)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        # AttributeError — `null` вместо объекта (`"summary": null`): `.get` у None.
        raise WeatherUnavailableError("answer does not match the compact format") from error


def condition_from_symbol(symbol: object) -> WeatherCondition | None:
    """Символ MET (`lightrainshowers_day`) → состояние неба; незнакомый — None."""
    if not isinstance(symbol, str):
        return None
    base = symbol.split("_", 1)[0]
    if "thunder" in base:
        return WeatherCondition.THUNDERSTORM
    if "sleet" in base:
        return WeatherCondition.SLEET
    if "snow" in base:
        return _by_intensity(
            base, WeatherCondition.LIGHT_SNOW, WeatherCondition.SNOW, WeatherCondition.HEAVY_SNOW
        )
    if "rain" in base:
        return _by_intensity(
            base, WeatherCondition.LIGHT_RAIN, WeatherCondition.RAIN, WeatherCondition.HEAVY_RAIN
        )
    return _PLAIN_SYMBOLS.get(base)


def _by_intensity(
    base: str, light: WeatherCondition, normal: WeatherCondition, heavy: WeatherCondition
) -> WeatherCondition:
    if base.startswith("light"):
        return light
    if base.startswith("heavy"):
        return heavy
    return normal


def _hour(entry: dict[str, Any]) -> WeatherHour:
    data = entry["data"]
    details = data["instant"]["details"]
    next_hour = data["next_1_hours"]
    precipitation = next_hour.get("details", {}).get("precipitation_amount")
    return WeatherHour(
        starts_at=datetime.fromisoformat(entry["time"]),
        air_temperature_c=_number(details["air_temperature"]),
        wind_speed_ms=_number(details["wind_speed"]),
        condition=condition_from_symbol(next_hour.get("summary", {}).get("symbol_code")),
        precipitation_mm=None if precipitation is None else _number(precipitation),
    )


def _number(value: object) -> float:
    # bool — подкласс int: `true` вместо температуры — не тот формат, а не 1 °C.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    return float(value)


def _coordinate(value: float) -> str:
    return f"{value:.{COORDINATE_DECIMALS}f}"


def _expires_at(header: str | None, now: datetime) -> datetime:
    try:
        expires = parsedate_to_datetime(header) if header else None
    except (TypeError, ValueError):
        expires = None
    if expires is None or expires.utcoffset() is None:
        return now + _DEFAULT_TTL
    return min(max(expires, now + _MIN_TTL), now + _MAX_TTL)


async def _read_capped(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > _MAX_RESPONSE_BYTES:
            raise WeatherUnavailableError("answer is larger than expected")
    return bytes(body)


def _upcoming(forecast: WeatherForecast, now: datetime) -> WeatherForecast:
    upcoming = without_finished_hours(forecast, now)
    if upcoming is None:
        raise WeatherUnavailableError("every hour of the forecast has finished")
    return upcoming


def _stale_or_raise(cached: _CachedForecast | None, now: datetime, reason: str) -> WeatherForecast:
    if cached is not None and now - cached.confirmed_at < STALE_GRACE:
        logger.info(
            "concierge_source_stale_copy",
            source="weather",
            age_seconds=int((now - cached.confirmed_at).total_seconds()),
        )
        return _upcoming(cached.forecast, now)
    raise WeatherUnavailableError(f"MET Norway forecast is unavailable: {reason}")
