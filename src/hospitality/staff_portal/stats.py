"""Данные страницы «Статистика» (spec 0065, issue #704): итоги по сотруднику за период.

Числа считает владелец — `modules/requests.staff_stats` из журнала изменений;
страница добавляет к ним имена из «Сотрудников» (`platform.staff_team`) и
раскладывает по колонкам. Рейтинга нет: сортировка — по имени, а не по числам
(тело issue).

Старший службы видит итоги своих служб (spec 0065 §2.3): граница — `scope`, та
же, что у сводки дня и журнала, срез — у владельца чисел. Период — «Сегодня» ·
«7 дней» · «30 дней» ссылками, сутки — по поясу отеля (FOUNDATION §9).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.humanize import duration_label_ru
from hospitality.staff_portal import checkin, scope

# Период → подпись и число суток отеля, включая сегодняшние. Порядок — порядок ссылок.
PERIODS: Final[dict[str, tuple[str, int]]] = {
    "today": ("Сегодня", 1),
    "7d": ("7 дней", 7),
    "30d": ("30 дней", 30),
}
# Утром «Сегодня» пусто — открывается неделя (spec 0065 §2.4).
DEFAULT_PERIOD: Final = "7d"
_NO_LOGIN_LABEL: Final = "без логина"


def section_enabled() -> bool:
    """`ENABLE_STAFF_STATS` (spec 0065 §2.4): одна проверка на меню и страницу —
    выключено, и раздела нет совсем."""
    return get_settings().enable_staff_stats


def parse_stats_period(raw: str | None) -> str:
    """Период из query-параметра; непонятное — умолчание, без 422 (канон
    `summary.parse_summary_day`): ссылку с опечаткой мог прислать коллега."""
    return raw if raw is not None and raw in PERIODS else DEFAULT_PERIOD


def stats_path(tenant_slug: str, *, period: str) -> str:
    """URL страницы — переключатель периода ссылками."""
    query = "" if period == DEFAULT_PERIOD else f"?period={period}"
    return f"/staff/{tenant_slug}/stats{query}"


def period_bounds(period: str, *, now: datetime, zone: tzinfo) -> tuple[datetime, datetime]:
    """`[полночь отеля N−1 суток назад, сейчас)` в UTC.

    Полночь — локальной даты отеля, а не «сейчас минус N×24 ч» (канон
    `day_summary._local_midnight_utc`): «Сегодня» в 09:00 — с полуночи отеля.
    """
    days = PERIODS[period][1]
    first_day = now.astimezone(zone).date() - timedelta(days=days - 1)
    return datetime.combine(first_day, time.min, tzinfo=zone).astimezone(UTC), now


async def build_stats_context(staff: StaffContext, *, period: str) -> dict[str, Any]:
    """Контекст шаблона `stats.html`. Вызывается внутри контекста тенанта запроса:
    журнал и заявки владелец чисел читает под RLS, чужих строк здесь нет."""
    zone = await checkin.hotel_zone(staff)
    since, until = period_bounds(period, now=utc_now(), zone=zone)
    categories = await requests_api.list_categories()
    totals = await requests_api.staff_stats(
        since=since, until=until, category_ids=scope.allowed_category_ids(staff, categories)
    )
    members = {
        member.user_id: member for member in await staff_team.list_tenant_members(staff.tenant_id)
    }
    rows = [_row(item, members.get(item.user_id)) for item in totals]
    rows.sort(key=lambda row: (str(row["name"]).casefold(), str(row["login"] or "")))
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "own_services": scope.own_services_label(staff, categories),
        "tabs": [
            {
                "label": label,
                "href": stats_path(staff.tenant_slug, period=key),
                "active": key == period,
            }
            for key, (label, _) in PERIODS.items()
        ],
        "rows": rows,
        # Один конфиг на все строки: у отеля без напоминаний «—» во всей колонке.
        "reminders_off": bool(totals) and totals[0].overdue is None,
    }


def _row(
    item: requests_api.StaffStatsRow, member: staff_team.TenantMemberView | None
) -> dict[str, Any]:
    """Строка таблицы. Имя и логин — текущие, из «Сотрудников»; учётку стёр
    суперадмин (её там нет) — вместо имени логин из журнала (spec 0065 §2.3)."""
    if member is not None:
        name, login = member.display_name, member.login
    else:
        name, login = item.login or _NO_LOGIN_LABEL, None
    return {
        "name": name,
        "login": login,
        "claimed": item.claimed,
        "done": item.done,
        "claim_wait": _duration(item.claim_wait_median_seconds),
        "work": _duration(item.work_median_seconds),
        "overdue": "—" if item.overdue is None else str(item.overdue),
        "returned": item.returned,
    }


def _duration(seconds: int | None) -> str:
    """Медиана словами, как в сводке дня; мерить нечего — прочерк, а не «0 сек»."""
    return "—" if seconds is None else duration_label_ru(seconds)
