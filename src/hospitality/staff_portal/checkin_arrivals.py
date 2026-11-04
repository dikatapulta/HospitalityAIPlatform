"""Блок «Заселены за день» страницы заселения (#832).

Справа от формы на компьютере, под ней на телефоне: проживания, заселённые в
выбранный день отеля (по его поясу из конфига тенанта, FOUNDATION §9), свежие
сверху, и подключился ли гость к чату. Строка — только комната, ночи, время
заселения и признак подключения: новых ПД блок не показывает.

День — параметр адреса `?day=ГГГГ-ММ-ДД`, без JS: чипы «Сегодня» и «Вчера» —
ссылки, «Другой день» — GET-форма. Непонятный или будущий день — «сегодня», а
не 422 (канон `changes.parse_filters`): ссылку с опечаткой мог прислать коллега.
Здесь только чистые расчёты; чтение — `checkin.build_checkin_context` (сутки отеля
`[00:00, 24:00)` по его поясу, как у журнала изменений).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Final
from urllib.parse import quote

from hospitality.modules.guests import api as guests_api
from hospitality.shared.humanize import month_day_ru, plural

# Границы правдоподобного дня — как у журнала изменений: дальше — мусор, и
# `date.max` + сутки дали бы 500 вместо страницы.
_MIN_YEAR: Final = 2000

EMPTY_TODAY: Final = (
    "Сегодня ещё никого не заселили. Здесь будет видно, подключился ли гость к чату."
)
EMPTY_PAST: Final = "В этот день никого не заселяли."


@dataclass(frozen=True)
class ArrivalStatus:
    """Признак подключения: ключ для CSS и текст строки."""

    key: str
    label: str


CONNECTED: Final = ArrivalStatus("connected", "Гость подключился")
WAITING: Final = ArrivalStatus("waiting", "Ждём гостя")
NOT_CONNECTED: Final = ArrivalStatus("not-connected", "Не подключился")


def parse_arrivals_day(raw: str | None, *, today: date) -> date:
    """День из `?day=`; пусто, опечатка, будущее или древность — «сегодня»."""
    try:
        day = date.fromisoformat(raw or "")
    except ValueError:
        return today
    if day > today or day.year < _MIN_YEAR:
        return today
    return day


def arrivals_heading(day: date, *, today: date) -> str:
    """«Заселены сегодня», «Заселены вчера», «Заселены 3 ноября» (прошлый год — с годом)."""
    if day == today:
        return "Заселены сегодня"
    if day == today - timedelta(days=1):
        return "Заселены вчера"
    label = month_day_ru(day)
    if day.year != today.year:
        label = f"{label} {day.year}"
    return f"Заселены {label}"


def nights_label(stay: guests_api.StayRead, zone: tzinfo) -> str:
    """«2 ночи» — даты заезда и выезда по отелю; у выехавшего — сколько прожил."""
    nights = (
        stay.check_out_at.astimezone(zone).date() - stay.check_in_at.astimezone(zone).date()
    ).days
    nights = max(nights, 0)
    return f"{nights} {plural(nights, 'ночь', 'ночи', 'ночей')}"


def arrival_status(stay: guests_api.StayRead, connected: bool, now: datetime) -> ArrivalStatus:
    """Подключился; не подключался, но ещё может («Ждём гостя»); уже не сможет.

    Ждать можно только действующее проживание: Stay в `checked_in` и срок не
    вышел. Просроченному Stay гость войти уже не может (`resolve_session`
    проверяет срок), выехавшему — тем более.
    """
    if connected:
        return CONNECTED
    if stay.status is guests_api.StayStatus.CHECKED_IN and stay.check_out_at > now:
        return WAITING
    return NOT_CONNECTED


def arrival_rows(
    arrivals: list[guests_api.StayArrival], tenant_slug: str, zone: tzinfo, now: datetime
) -> list[dict[str, Any]]:
    """Строки списка: «Комната 1512 · 2 ночи · 14:05 · ✓ Гость подключился».

    `href` — та же карточка с QR и кодом, что поиск (`?room=`); у выехавшего —
    `None`: карточки у закрытого Stay нет, строка без действий.
    """
    rows: list[dict[str, Any]] = []
    for arrival in arrivals:
        stay = arrival.stay
        checked_out = stay.status is guests_api.StayStatus.CHECKED_OUT
        rows.append(
            {
                "room_number": stay.room_number,
                "nights": nights_label(stay, zone),
                "time": f"{stay.check_in_at.astimezone(zone):%H:%M}",
                "status": arrival_status(stay, arrival.guest_connected, now),
                "checked_out": checked_out,
                "href": (
                    None
                    if checked_out
                    else f"/staff/{tenant_slug}/checkin?room={quote(stay.room_number, safe='')}"
                ),
            }
        )
    return rows


def _day_href(tenant_slug: str, day: date, today: date) -> str:
    base = f"/staff/{tenant_slug}/checkin"
    return base if day == today else f"{base}?day={day.isoformat()}"


def arrivals_context(
    arrivals: list[guests_api.StayArrival],
    *,
    day: date,
    tenant_slug: str,
    zone: tzinfo,
    now: datetime,
) -> dict[str, Any]:
    """Контекст блока для `checkin.html`: заголовок, строки, пустой день, выбор дня."""
    today = now.astimezone(zone).date()
    yesterday = today - timedelta(days=1)
    return {
        "heading": arrivals_heading(day, today=today),
        "rows": arrival_rows(arrivals, tenant_slug, zone, now),
        "empty": EMPTY_TODAY if day == today else EMPTY_PAST,
        "chips": [
            {
                "label": "Сегодня",
                "href": _day_href(tenant_slug, today, today),
                "active": day == today,
            },
            {
                "label": "Вчера",
                "href": _day_href(tenant_slug, yesterday, today),
                "active": day == yesterday,
            },
        ],
        "day_value": day.isoformat(),
        "max_day": today.isoformat(),
    }
