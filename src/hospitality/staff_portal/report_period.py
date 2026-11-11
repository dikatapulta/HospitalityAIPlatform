"""Период листа «Отчёт» (spec 0083 §2.2): прошлая неделя, прошлый месяц или свои даты.

Чистые функции от «сегодня отеля» — без базы и часов: `today` даёт страница по поясу
отеля (FOUNDATION §9), поэтому границы проверяются тестом без подмены времени.
Неделя и месяц — законченные календарные; сравнение — такой же период прямо перед
выбранным. Ошибка в своих датах — прошлая неделя с надписью, без 422 (канон
`summary.parse_summary_day`): ссылку с опечаткой мог прислать коллега.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from hospitality.shared.humanize import month_day_ru

PERIOD_WEEK: Final = "week"
PERIOD_MONTH: Final = "month"
PERIOD_CUSTOM: Final = "custom"
# Порядок — порядок переключателя (§4).
PERIOD_TITLES: Final = {
    PERIOD_WEEK: "Прошлая неделя",
    PERIOD_MONTH: "Прошлый месяц",
    PERIOD_CUSTOM: "Свои даты",
}
MAX_CUSTOM_DAYS: Final = 92
CUSTOM_ERROR: Final = "Период — от одного дня до 92 дней и не позже сегодняшнего."


@dataclass(frozen=True)
class ReportPeriod:
    """Дни листа и дни сравнения — включительно, локальные даты отеля."""

    key: str
    first_day: date
    last_day: date
    previous_first: date
    previous_last: date
    error: str | None = None


def parse_report_period(params: Mapping[str, str], *, today: date) -> ReportPeriod:
    """Период из query-параметров `period`, `from`, `to`; непонятное — прошлая неделя."""
    key = params.get("period")
    if key == PERIOD_MONTH:
        return _previous_month(today)
    if key == PERIOD_CUSTOM:
        custom = _custom(params.get("from"), params.get("to"), today=today)
        if custom is not None:
            return custom
        week = _previous_week(today)
        return ReportPeriod(
            PERIOD_WEEK,
            week.first_day,
            week.last_day,
            week.previous_first,
            week.previous_last,
            error=CUSTOM_ERROR,
        )
    return _previous_week(today)


def report_path(tenant_slug: str, key: str) -> str:
    """URL листа: у недели — без параметра (умолчание), у своих дат — форма GET."""
    base = f"/staff/{tenant_slug}/report"
    return base if key in (PERIOD_WEEK, PERIOD_CUSTOM) else f"{base}?period={key}"


def range_label(first: date, last: date) -> str:
    """«2–8 ноября 2026», «26 октября – 1 ноября 2026», «29 декабря 2025 – 4 января 2026»."""
    if first == last:
        return f"{month_day_ru(first)} {first.year}"
    if first.year != last.year:
        return f"{month_day_ru(first)} {first.year} – {month_day_ru(last)} {last.year}"
    if first.month != last.month:
        return f"{month_day_ru(first)} – {month_day_ru(last)} {last.year}"
    return f"{first.day}–{month_day_ru(last)} {last.year}"


def _previous_week(today: date) -> ReportPeriod:
    """Понедельник–воскресенье перед текущей неделей; сравнение — неделя перед ней."""
    first = today - timedelta(days=today.weekday() + 7)
    return ReportPeriod(
        PERIOD_WEEK,
        first,
        first + timedelta(days=6),
        first - timedelta(days=7),
        first - timedelta(days=1),
    )


def _previous_month(today: date) -> ReportPeriod:
    """Прошлый календарный месяц; сравнение — месяц перед ним (длины могут не совпадать)."""
    last = today.replace(day=1) - timedelta(days=1)
    first = last.replace(day=1)
    previous_last = first - timedelta(days=1)
    return ReportPeriod(PERIOD_MONTH, first, last, previous_last.replace(day=1), previous_last)


def _custom(raw_first: str | None, raw_last: str | None, *, today: date) -> ReportPeriod | None:
    """Свои даты включительно: 1–92 дня, не позже сегодня; иначе `None`."""
    try:
        first = date.fromisoformat(raw_first or "")
        last = date.fromisoformat(raw_last or "")
    except ValueError:
        return None
    days = (last - first).days + 1
    if not 1 <= days <= MAX_CUSTOM_DAYS or last > today:
        return None
    previous_last = first - timedelta(days=1)
    return ReportPeriod(
        PERIOD_CUSTOM, first, last, previous_last - timedelta(days=days - 1), previous_last
    )
