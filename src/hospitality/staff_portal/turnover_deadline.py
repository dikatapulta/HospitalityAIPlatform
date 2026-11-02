"""«Срочно к часу» на доске выездов (spec 0062 §2): время по поясу отеля и надписи.

Метку хранит модуль `housekeeping` моментом UTC (`needed_by_at`); здесь — то,
что знает только кабинет: «11:00» из поля — это сегодня по часам отеля
(FOUNDATION §9), и как метку читают доска, сигнал и пуш. Одни слова на всех
троих (P-12): «Срочно к 11:00» на доске, «1504 — срочно к 11:00» в уведомлении.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Final

# «11:00» из поля «Нужен к»; секунды браузер шлёт, если у поля шаг меньше минуты.
CLOCK_PATTERN: Final = r"^([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?$"
_CLOCK: Final = re.compile(CLOCK_PATTERN)


def today_at(clock_text: str, zone: tzinfo, now: datetime) -> datetime:
    """«11:00» → сегодняшние 11:00 по поясу отеля, моментом UTC.

    Через полночь метку не ставят (spec 0062 §2): ночью номер к утру отмечают
    утром, поэтому «01:00» в 23:30 — уже прошедший час сегодняшнего дня, и домен
    ответит ERR-HOUSEKEEPING-007.
    """
    if not _CLOCK.fullmatch(clock_text):
        raise ValueError(f"clock not understood: {clock_text!r}")
    local_day = now.astimezone(zone).date()
    moment = time(int(clock_text[0:2]), int(clock_text[3:5]))
    return datetime.combine(local_day, moment, tzinfo=zone).astimezone(UTC)


def default_clock(zone: tzinfo, now: datetime) -> str:
    """Ближайший целый час по отелю для поля «Нужен к»: в 10:20 — «11:00».

    После 23:00 следующий целый час — завтра: поле остаётся пустым.
    """
    local = now.astimezone(zone)
    next_hour = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return f"{next_hour:%H:00}" if next_hour.date() == local.date() else ""


def clock_value(needed_by_at: datetime, zone: tzinfo) -> str:
    """«11:00» текущей метки — поле «Изменить время» открывается с ней."""
    return f"{needed_by_at.astimezone(zone):%H:%M}"


def short_label(needed_by_at: datetime, zone: tzinfo, now: datetime) -> str:
    """«11:00» сегодня, «11:00, 02.11» — метка висит со вчера (сигнал, пуш, доска)."""
    local = needed_by_at.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return f"{local:%H:%M}"
    return f"{local:%H:%M, %d.%m}"


def board_label(needed_by_at: datetime, zone: tzinfo, now: datetime) -> str:
    """Метка на карточке номера (spec 0062 §2): «Срочно к 11:00»; срок прошёл —
    «Срочно к 11:00 · время вышло»; срок не сегодня — «Срочно к 11:00, 02.11»."""
    label = f"Срочно к {short_label(needed_by_at, zone, now)}"
    is_today = needed_by_at.astimezone(zone).date() == now.astimezone(zone).date()
    if is_today and needed_by_at <= now:
        return f"{label} · время вышло"
    return label
