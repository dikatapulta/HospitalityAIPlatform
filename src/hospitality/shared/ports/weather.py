"""Порт погоды консьержа (spec 0084, FOUNDATION §8, P-3).

Устроен по канону `shared/ports/file_storage.py`: `typing.Protocol` с
async-методами, исключения порта рядом, адаптеры — `integrations/weather/`
(Fake обязателен, ADR-007), общий контрактный набор `tests/test_weather_port.py`,
адаптер в порт кладёт только composition root (`set_weather`).

Порт знает точку на карте и отдаёт почасовой прогноз; о тенантах, городах и
поясах он не знает: точку берёт из конфига отеля вызывающий, сутки по поясу
отеля считает тоже он. Кэш и паузу после отказа держит адаптер — это условия
провайдера (spec 0084 Р-3).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Final, Protocol

# MET Norway отвечает 403 на 5 знаков и больше; 4 знака — около 11 м, для погоды с запасом.
COORDINATE_DECIMALS: Final = 4
_HOUR: Final = timedelta(hours=1)


class WeatherUnavailableError(Exception):
    """Прогноза нет: нет соединения, таймаут, отказ провайдера или ответ не по формату."""


class WeatherNotConfiguredError(RuntimeError):
    """Composition root не положил адаптер — программная ошибка сборки процесса."""


class WeatherCondition(enum.StrEnum):
    """Что за окном в этот час — общий словарь всех адаптеров (spec 0084 §3)."""

    CLEAR = "clear"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    FOG = "fog"
    LIGHT_RAIN = "light_rain"
    RAIN = "rain"
    HEAVY_RAIN = "heavy_rain"
    SLEET = "sleet"
    LIGHT_SNOW = "light_snow"
    SNOW = "snow"
    HEAVY_SNOW = "heavy_snow"
    THUNDERSTORM = "thunderstorm"


@dataclass(frozen=True)
class GeoPoint:
    """Точка на карте; координаты округляются до `COORDINATE_DECIMALS` при создании."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.latitude) and -90 <= self.latitude <= 90):
            raise ValueError("latitude must be within -90..90")
        if not (math.isfinite(self.longitude) and -180 <= self.longitude <= 180):
            raise ValueError("longitude must be within -180..180")
        object.__setattr__(self, "latitude", round(self.latitude, COORDINATE_DECIMALS))
        object.__setattr__(self, "longitude", round(self.longitude, COORDINATE_DECIMALS))


@dataclass(frozen=True)
class WeatherHour:
    """Один час прогноза: с `starts_at` (UTC) на час вперёд."""

    starts_at: datetime
    air_temperature_c: float
    wind_speed_ms: float
    # None — код провайдера незнаком: адаптер не угадывает, блок промолчит о небе.
    condition: WeatherCondition | None = None
    precipitation_mm: float | None = None

    def __post_init__(self) -> None:
        if self.starts_at.utcoffset() is None:
            raise ValueError("starts_at must be timezone-aware")
        if not math.isfinite(self.air_temperature_c):
            raise ValueError("air_temperature_c must be finite")
        if not (math.isfinite(self.wind_speed_ms) and self.wind_speed_ms >= 0):
            raise ValueError("wind_speed_ms must be a non-negative number")
        if self.precipitation_mm is not None and not (
            math.isfinite(self.precipitation_mm) and self.precipitation_mm >= 0
        ):
            raise ValueError("precipitation_mm must be a non-negative number")


@dataclass(frozen=True)
class WeatherForecast:
    """Прогноз в точке: `updated_at` — когда провайдер его посчитал, часы — по возрастанию."""

    updated_at: datetime
    hours: tuple[WeatherHour, ...]

    def __post_init__(self) -> None:
        if self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        if not self.hours:
            raise ValueError("forecast must have at least one hour")
        starts = [hour.starts_at for hour in self.hours]
        if any(later <= earlier for earlier, later in pairwise(starts)):
            raise ValueError("forecast hours must be strictly increasing")


def without_finished_hours(forecast: WeatherForecast, now: datetime) -> WeatherForecast | None:
    """Прогноз без часов, закончившихся к `now`; не осталось ни одного — None.

    Одно правило для всех адаптеров (P-12): прогноз из кэша или Fake не отдаёт гостю
    «сейчас» трёхчасовой давности.
    """
    hours = tuple(hour for hour in forecast.hours if hour.starts_at + _HOUR > now)
    return replace(forecast, hours=hours) if hours else None


class WeatherPort(Protocol):
    """Почасовой прогноз погоды в точке.

    Контракт (общие тесты — `tests/test_weather_port.py`):
    - `forecast` отдаёт часы от текущего по возрастанию, хотя бы один, время — UTC;
      закончившиеся часы отброшены (`without_finished_hours`);
    - нет прогноза — `WeatherUnavailableError`, а не выдуманный или пустой прогноз;
    - `check` — health адаптера (FOUNDATION §8 правило 1): `True`, если провайдер отвечает.
    """

    backend: str

    async def forecast(self, point: GeoPoint) -> WeatherForecast: ...

    async def check(self) -> bool: ...


_weather: WeatherPort | None = None


def set_weather(weather: WeatherPort) -> None:
    """Положить адаптер процесса. Зовёт только composition root (и тесты)."""
    global _weather
    _weather = weather


def get_weather() -> WeatherPort:
    """Адаптер процесса; не положен — `WeatherNotConfiguredError`."""
    if _weather is None:
        raise WeatherNotConfiguredError(
            "weather is not configured: composition root must call set_weather()"
        )
    return _weather


def weather_or_none() -> WeatherPort | None:
    """То же без исключения — тестам, которым нужно вернуть прежний адаптер."""
    return _weather
