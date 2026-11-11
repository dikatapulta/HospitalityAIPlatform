"""Адаптеры порта погоды (spec 0084) и выбор по настройкам."""

from __future__ import annotations

from hospitality.integrations.weather.fake import FakeWeatherAdapter
from hospitality.integrations.weather.met_norway import MetNorwayWeatherAdapter
from hospitality.shared.config import Settings
from hospitality.shared.ports.weather import WeatherPort

__all__ = ["FakeWeatherAdapter", "MetNorwayWeatherAdapter", "build_weather"]


def build_weather(settings: Settings) -> WeatherPort:
    """Адаптер по `WEATHER_BACKEND` — зовёт только composition root."""
    if settings.weather_backend == "met_norway":
        return MetNorwayWeatherAdapter(settings.weather_user_agent)
    return FakeWeatherAdapter()
