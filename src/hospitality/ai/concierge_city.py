"""Блок хода «Weather and exchange rates»: погода и официальный курс (spec 0084 §4).

Блок, а не инструмент, — по той же причине, что `# Staying longer`: ход
однократный, и «узнать и ответить» инструментом потребовало бы второго обращения
к модели (spec 0084 Р-1). Данные — через порты `shared/ports/weather.py` и
`shared/ports/exchange_rates.py`: адаптеры из `ai/` не импортируются (контракт 2).

Пустая строка — блока нет: флаг `ENABLE_CONCIERGE_CITY` выключен, конфига хода
нет или у отеля нет `concierge_city`. Источник не ответил — его строка говорит
модели «данных нет», ход идёт (FOUNDATION §8 правило 2). Текст блока пересказан
в spec 0084 §4: правишь здесь — правишь там тем же PR.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, tzinfo
from typing import Final

from hospitality.platform.config import TenantConfig
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.ports.exchange_rates import (
    ExchangeRatesUnavailableError,
    OfficialRates,
    get_exchange_rates,
)
from hospitality.shared.ports.weather import (
    GeoPoint,
    WeatherForecast,
    WeatherHour,
    WeatherUnavailableError,
    get_weather,
)

logger = get_logger(module=__name__)

# Предел ожидания источника на ходу гостя. Адаптер сам укладывает вызов в 3 с и после
# отказа закрывает провайдера на 60 с (spec 0084 Р-3); предел блока длиннее, чтобы
# сработал сначала таймаут адаптера с его паузой, а не отмена отсюда.
SOURCE_DEADLINE_SECONDS: Final = 4.0
# Шаг прогноза в строках дней, часы по поясу отеля: 00:00, 03:00, … 21:00.
_STEP_HOURS: Final = 3
# Валюты гостей отеля — в этом порядке; остальные строки банка модели не нужны.
CURRENCIES: Final = (
    "USD",
    "EUR",
    "RUB",
    "CNY",
    "GBP",
    "KGS",
    "UZS",
    "TRY",
    "KRW",
    "JPY",
    "AED",
    "INR",
)
_SEPARATOR: Final = " · "

# Имена — свои, а не `%A`/`%B` strftime: те зависят от локали процесса.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_RULES: Final = (
    "Use this only when the guest asks about the weather, what to wear, or about exchange "
    "rates. Do not bring it up yourself. When you give the weather, say once, briefly, that "
    "it is a MET Norway forecast. When you give a rate, say it is the official National Bank "
    "of Kazakhstan rate for that date and that exchange offices and the hotel's front desk "
    "have their own rates. Never convert the hotel's prices or the guest's bill with these "
    "rates: what the guest pays, the front desk tells them."
)


async def concierge_city_block(config: TenantConfig | None, *, now: datetime | None = None) -> str:
    """Блок хода при включённом флаге и городе в конфиге; иначе пустая строка.

    Оба источника спрашиваются параллельно, каждый — не дольше
    `SOURCE_DEADLINE_SECONDS`; любой отказ источника — строка «данных нет».
    """
    if not get_settings().enable_concierge_city or config is None:
        return ""
    city = config.concierge_city
    if city is None:
        return ""
    moment = now or utc_now()
    zone = config.tzinfo
    today = moment.astimezone(zone).date()
    point = GeoPoint(city.latitude, city.longitude)
    forecast, rates = await asyncio.gather(_forecast(point), _official_rates(today))
    logger.info("concierge_city_in_context", weather=forecast is not None, rates=rates is not None)
    return render_concierge_city_block(city.name, forecast, rates, now=moment, zone=zone)


def render_concierge_city_block(
    city_name: str,
    forecast: WeatherForecast | None,
    rates: OfficialRates | None,
    *,
    now: datetime,
    zone: tzinfo,
) -> str:
    """Текст блока: часы и сутки — по поясу отеля; нет данных источника — строка «нет»."""
    weather = _weather_text(city_name, forecast, now, zone)
    exchange = _rates_text(rates)
    return f"\n\n# Weather and exchange rates\n\n{weather}\n\n{exchange}\n\n{_RULES}"


async def _forecast(point: GeoPoint) -> WeatherForecast | None:
    try:
        async with asyncio.timeout(SOURCE_DEADLINE_SECONDS):
            return await get_weather().forecast(point)
    except WeatherUnavailableError:
        return None  # адаптер уже записал отказ: лог, метрика, пауза
    except Exception as error:  # ход гостя дороже погоды: любой сбой — «данных нет»
        logger.warning(
            "concierge_block_source_failed", source="weather", error_type=type(error).__name__
        )
        return None


async def _official_rates(today: date) -> OfficialRates | None:
    try:
        async with asyncio.timeout(SOURCE_DEADLINE_SECONDS):
            return await get_exchange_rates().official_rates(today)
    except ExchangeRatesUnavailableError:
        return None
    except Exception as error:
        logger.warning(
            "concierge_block_source_failed",
            source="exchange_rates",
            error_type=type(error).__name__,
        )
        return None


def _weather_text(
    city_name: str, forecast: WeatherForecast | None, now: datetime, zone: tzinfo
) -> str:
    lines = _weather_lines(forecast, now, zone) if forecast is not None else []
    if forecast is None or not lines:
        return (
            f"Weather forecast for {city_name} is not available right now: "
            "say so honestly, do not guess."
        )
    updated = _updated(forecast.updated_at, now, zone)
    header = f"Weather forecast for {city_name} by MET Norway, hotel local time ({updated}):"
    return "\n".join([header, *lines])


def _weather_lines(forecast: WeatherForecast, now: datetime, zone: tzinfo) -> list[str]:
    """«- now: …» по текущему часу и строка на сегодня и завтра — каждые 3 часа."""
    lines: list[str] = []
    current = forecast.hours[0]
    if current.starts_at <= now:
        lines.append(f"- now: {_hour_now(current)}")
    today = now.astimezone(zone).date()
    for day in (today, today + timedelta(days=1)):
        slots = [
            hour
            for hour in forecast.hours
            if hour.starts_at > now
            and hour.starts_at.astimezone(zone).date() == day
            and hour.starts_at.astimezone(zone).hour % _STEP_HOURS == 0
        ]
        if slots:
            entries = _SEPARATOR.join(_hour_slot(hour, zone) for hour in slots)
            lines.append(f"- {_day(day)}: {entries}")
    return lines


def _hour_now(hour: WeatherHour) -> str:
    parts = [_temperature(hour)]
    if hour.condition is not None:
        parts.append(_condition(hour))
    parts.append(f"wind {round(hour.wind_speed_ms)} m/s")
    return ", ".join(parts)


def _hour_slot(hour: WeatherHour, zone: tzinfo) -> str:
    text = f"{hour.starts_at.astimezone(zone):%H:%M} {_temperature(hour)}"
    return f"{text} {_condition(hour)}" if hour.condition is not None else text


def _temperature(hour: WeatherHour) -> str:
    # `round` отдаёт int: −0,4 °C — «0 °C», а не «-0 °C».
    return f"{round(hour.air_temperature_c)} °C"


def _condition(hour: WeatherHour) -> str:
    return hour.condition.value.replace("_", " ") if hour.condition is not None else ""


def _rates_text(rates: OfficialRates | None) -> str:
    unavailable = (
        "Official exchange rates are not available right now: say so honestly, do not guess."
    )
    if rates is None:
        return unavailable
    listed = [rate for rate in map(rates.find, CURRENCIES) if rate is not None]
    if not listed:
        return unavailable
    values = _SEPARATOR.join(f"{rate.units} {rate.currency} = {rate.rate}" for rate in listed)
    return (
        "Official exchange rates of the National Bank of Kazakhstan for "
        f"{_date(rates.effective_date)}, in tenge (KZT):\n{values}"
    )


def _updated(updated_at: datetime, now: datetime, zone: tzinfo) -> str:
    local = updated_at.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return f"updated {local:%H:%M}"
    return f"updated {local.day} {_MONTHS[local.month - 1]} {local:%H:%M}"


def _day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"


def _date(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"
