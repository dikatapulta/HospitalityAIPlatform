"""Данные страницы «Журнал изменений» (spec 0041 §2.4, §4; issue #370).

Читает журнал ядра (`platform.staff_actions`) за один день отеля и
раскладывает строки по колонкам «Время · Сотрудник · Действие · Что».
Подписи действий и формат «Что» живут здесь: журнал хранит значения и
снапшоты, слова для менеджера — забота страницы.

Старший службы видит строки своих служб (spec 0045 §3): служба «откуда» или
«куда» — одна из его служб, срез — в SQL журнала (`category_ids`), граница —
`scope`, та же, что у очереди. Строк заселения у него нет, как и пунктов
фильтра о них; логины фильтра — только из строк его служб.

Фильтры — GET-форма без JS: день (по поясу отеля, по умолчанию сегодня),
сотрудник (логины из самого журнала), действие. Непонятное значение фильтра
не даёт 422, а сбрасывается к умолчанию (канон `summary.parse_summary_day`):
ссылку с опечаткой мог прислать коллега.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final
from urllib.parse import urlencode

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_actions
from hospitality.platform.staff_actions import AUTOMATIC_ACTIONS, StaffAction, StaffActionRead
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.staff_portal import checkin, scope

# Страница журнала — 200 строк, дальше «Показать более ранние» (§2.4).
PAGE_SIZE: Final = 200

# Подписи действий (§2.2) — порядок словаря задаёт порядок пунктов фильтра.
# Род не нужен: по логину не видно, кто сотрудник (§4).
ACTION_LABELS: Final[dict[StaffAction, str]] = {
    StaffAction.REQUEST_CREATED: "Создана вручную",
    StaffAction.REQUEST_CLAIMED: "Взята в работу",
    StaffAction.REQUEST_DONE: "Выполнена",
    StaffAction.REQUEST_CANCELLED: "Отменена",
    StaffAction.REQUEST_RETURNED: "Возвращена в очередь",
    StaffAction.REQUEST_TRANSFERRED: "Передана в другую службу",
    StaffAction.STAY_CHECKED_IN: "Заселение",
    StaffAction.STAY_CHECKED_OUT: "Выезд",
    StaffAction.STAY_MOVED: "Переселение",
    StaffAction.STAY_EXTENDED: "Продление",
    StaffAction.STAY_CODE_REISSUED: "Новый код",
    StaffAction.STAY_AUTO_CHECKED_OUT: "Выезд по сроку",
}
# Действия с заявкой — пункты фильтра у старшего: строк заселения он не видит.
_REQUEST_ACTIONS: Final = frozenset(
    {
        StaffAction.REQUEST_CREATED,
        StaffAction.REQUEST_CLAIMED,
        StaffAction.REQUEST_DONE,
        StaffAction.REQUEST_CANCELLED,
        StaffAction.REQUEST_RETURNED,
        StaffAction.REQUEST_TRANSFERRED,
    }
)
_DONE_WITH_NOTE_LABEL: Final = "Выполнена с оговоркой"
_NO_LOGIN_LABEL: Final = "без логина"
# Колонка «Сотрудник» у действия системы (spec 0043 §2.3): не «без логина» —
# это не учётка без логина, а отсутствие человека.
_AUTOMATIC_LABEL: Final = "автоматически"

# Границы правдоподобного дня и сдвига: дальше — не опечатка, а мусор, и
# `date.max` + сутки или сдвиг вне bigint дали бы 500 вместо страницы.
_MIN_YEAR: Final = 2000
_MAX_YEAR: Final = 2100
_MAX_OFFSET: Final = 1_000_000


@dataclass(frozen=True)
class ChangesFilters:
    """Разобранные фильтры запроса: день отеля, логин, действие, сдвиг."""

    day: date
    login: str | None
    action: StaffAction | None
    offset: int


def parse_filters(query: dict[str, str], *, today: date) -> ChangesFilters:
    """Фильтры из query-параметров; всё непонятное — умолчание, не 422."""
    try:
        day = date.fromisoformat(query.get("day", ""))
    except ValueError:
        day = today
    if not _MIN_YEAR <= day.year <= _MAX_YEAR:
        day = today
    try:
        action: StaffAction | None = StaffAction(query.get("action", ""))
    except ValueError:
        action = None
    raw_offset = query.get("offset", "")
    offset = min(int(raw_offset), _MAX_OFFSET) if raw_offset.isdecimal() else 0
    return ChangesFilters(day=day, login=query.get("login") or None, action=action, offset=offset)


def day_window_utc(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """Сутки отеля как полуинтервал UTC `[00:00, 24:00)` по его поясу (§2.4)."""
    start = datetime.combine(day, time(0), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


async def build_changes_context(staff: StaffContext, query: dict[str, str]) -> dict[str, Any]:
    """Контекст шаблона `changes.html`.

    Вызывается внутри контекста тенанта запроса — журнал читается под RLS,
    чужих строк на странице не окажется (P-4). Строк берётся на одну больше
    страницы: лишняя говорит, что есть «более ранние».
    """
    zone = await checkin.hotel_zone(staff)
    today = utc_now().astimezone(zone).date()
    filters = parse_filters(query, today=today)
    day_start, day_end = day_window_utc(filters.day, zone)
    categories = await requests_api.list_categories()
    category_ids = scope.allowed_category_ids(staff, categories)
    rows = await staff_actions.list_staff_actions(
        day_start=day_start,
        day_end=day_end,
        actor_login=filters.login,
        actions=frozenset({filters.action}) if filters.action is not None else None,
        limit=PAGE_SIZE + 1,
        offset=filters.offset,
        category_ids=category_ids,
    )
    logins = await staff_actions.list_staff_action_logins(category_ids=category_ids)
    has_more = len(rows) > PAGE_SIZE
    return {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "retention_days": get_settings().messages_retention_days,
        "own_services": scope.own_services_label(staff, categories),
        "day_value": filters.day.isoformat(),
        "login_options": logins,
        "selected_login": filters.login or "",
        "action_options": [
            {"value": action.value, "label": label}
            for action, label in ACTION_LABELS.items()
            if category_ids is None or action in _REQUEST_ACTIONS
        ],
        "selected_action": filters.action.value if filters.action is not None else "",
        "rows": [_row(row, zone, filters.day) for row in rows[:PAGE_SIZE]],
        "earlier_href": _earlier_href(staff.tenant_slug, filters) if has_more else None,
    }


def _row(row: StaffActionRead, zone: tzinfo, day: date) -> dict[str, str]:
    return {
        "time": row.occurred_at.astimezone(zone).strftime("%H:%M"),
        "login": (
            _AUTOMATIC_LABEL
            if row.action in AUTOMATIC_ACTIONS
            else row.actor_login or _NO_LOGIN_LABEL
        ),
        "action": action_label(row),
        "subject": subject_label(row, day),
    }


def action_label(row: StaffActionRead) -> str:
    """Подпись действия; «Выполнена» с примечанием — «с оговоркой» (§2.2)."""
    if row.action is StaffAction.REQUEST_DONE and row.with_note:
        return _DONE_WITH_NOTE_LABEL
    return ACTION_LABELS[row.action]


def subject_label(row: StaffActionRead, day: date) -> str:
    """Колонка «Что» (§2.2): заявка — `#12 · Уборка · 305`, Stay — комната.

    У передачи служба — «откуда → куда» (`#12 · Уборка → Инженер · 305`,
    spec 0042 §2.3), по образцу переселения.

    Заявка не из выбранного дня показывается с датой — `#12 (15.10)`: номер
    уникален только внутри дня, и вчерашнюю `#12` не отличить от сегодняшней.
    """
    if row.request_id is not None:
        number = f"#{row.request_daily_number}" if row.request_daily_number else "#?"
        if row.request_service_day is not None and row.request_service_day != day:
            number += f" ({row.request_service_day.strftime('%d.%m')})"
        service = row.category_name
        if row.action is StaffAction.REQUEST_TRANSFERRED:
            service = f"{row.category_name or '—'} → {row.category_name_to or '—'}"
        parts = [number, service, row.room_number]
        return " · ".join(part for part in parts if part)
    room = row.room_number or "—"
    if row.action is StaffAction.STAY_MOVED:
        return f"{room} → {row.room_number_to or '—'}"
    if row.action is StaffAction.STAY_EXTENDED and row.nights:
        return f"{room} · на {row.nights} {nights_word(row.nights)}"
    return room


def nights_word(count: int) -> str:
    """«ночь / ночи / ночей» по числу: 1 ночь, 2 ночи, 5 ночей, 21 ночь."""
    if count % 10 == 1 and count % 100 != 11:
        return "ночь"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "ночи"
    return "ночей"


def _earlier_href(tenant_slug: str, filters: ChangesFilters) -> str:
    """Ссылка «Показать более ранние» — те же фильтры, сдвиг на страницу."""
    params = {"day": filters.day.isoformat(), "offset": str(filters.offset + PAGE_SIZE)}
    if filters.login:
        params["login"] = filters.login
    if filters.action is not None:
        params["action"] = filters.action.value
    return f"/staff/{tenant_slug}/changes?{urlencode(params)}"
