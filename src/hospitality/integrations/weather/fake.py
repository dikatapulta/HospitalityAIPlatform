"""Fake-адаптер погоды (ADR-007, spec 0084): прогноз в памяти процесса.

Без настройки отдаёт любой точке правдоподобные 48 часов от текущего: температура
идёт по суткам вокруг 5 °C (холоднее всего в 05:00 UTC, теплее в 17:00), ветер —
3 м/с, небо — переменная облачность, без осадков. Так локальный uvicorn, CI и демо
работают без сети. `set_forecast(point, forecast)` — свой прогноз точке (контрактные
тесты, сценарии блока хода); `unavailable = True` — провайдер лежит. Закончившиеся
часы отбрасываются тем же правилом порта, что у настоящего адаптера.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta

from hospitality.shared.db import utc_now
from hospitality.shared.ports.weather import (
    GeoPoint,
    WeatherCondition,
    WeatherForecast,
    WeatherHour,
    WeatherUnavailableError,
    without_finished_hours,
)

_SYNTHETIC_HOURS = 48


class FakeWeatherAdapter:
    backend = "fake"

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.unavailable = False
        self._clock = clock
        self._forecasts: dict[GeoPoint, WeatherForecast] = {}

    def set_forecast(self, point: GeoPoint, forecast: WeatherForecast) -> None:
        """Точке `point` — этот прогноз вместо выдуманного."""
        self._forecasts[point] = forecast

    async def forecast(self, point: GeoPoint) -> WeatherForecast:
        if self.unavailable:
            raise WeatherUnavailableError("fake weather is marked unavailable")
        now = self._clock()
        stored = self._forecasts.get(point)
        forecast = without_finished_hours(stored, now) if stored is not None else None
        if stored is not None and forecast is None:
            raise WeatherUnavailableError("every hour of the stored forecast has finished")
        return forecast if forecast is not None else synthetic_forecast(now)

    async def check(self) -> bool:
        return not self.unavailable


def synthetic_forecast(now: datetime) -> WeatherForecast:
    """Выдуманный прогноз от часа `now`: одинаков для любой точки и дня."""
    first_hour = now.replace(minute=0, second=0, microsecond=0)
    starts = [first_hour + timedelta(hours=offset) for offset in range(_SYNTHETIC_HOURS)]
    hours = tuple(
        WeatherHour(
            starts_at=starts_at,
            air_temperature_c=round(5 - 4 * math.cos((starts_at.hour - 5) * math.pi / 12), 1),
            wind_speed_ms=3.0,
            condition=WeatherCondition.PARTLY_CLOUDY,
            precipitation_mm=0.0,
        )
        for starts_at in starts
    )
    return WeatherForecast(updated_at=first_hour, hours=hours)
