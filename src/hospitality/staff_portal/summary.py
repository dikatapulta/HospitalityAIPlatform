"""Данные страницы «Сводка дня» (spec 0035 §6–§7, PR D серии — issue #300).

Складывает числа, каждое из которых посчитал владелец своих данных (§6, P-5):
заявки — `modules/requests.day_summary`, эскалации — `channels/common`.
Отдельного «модуля отчётов» здесь нет и не заводится: страница только
раскладывает готовые числа по плиткам.

Расхода на модель на этой странице нет намеренно (§7/§8): себестоимость
платформы — не число отеля, оно уходит копией сводки в платформенный
алерт-чат (PR E серии, #301).

Один день на экран, переключатель «Сегодня / Вчера» — оба дня локальные,
по часовому поясу отеля. Окно суток для эскалаций берётся не из своего
расчёта, а из ответа `day_summary`: у обоих чисел обязано быть ровно одно окно.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

from hospitality.channels.common.events import count_escalations
from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.staff_portal import checkin

SUMMARY_DAY_TODAY: Final = "today"
SUMMARY_DAY_YESTERDAY: Final = "yesterday"

# Месяцы в родительном падеже: «29 августа». `%B` дал бы именительный
# («август») и зависел бы от локали процесса, которой в контейнере нет.
_MONTHS_GENITIVE: Final = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def parse_summary_day(raw: str | None) -> str:
    """День из query-параметра; всё непонятное — «сегодня».

    Не 422: ссылку с опечаткой мог прислать коллега, страница обязана
    открыться (канон `queue.parse_queue_tab`).
    """
    return SUMMARY_DAY_YESTERDAY if raw == SUMMARY_DAY_YESTERDAY else SUMMARY_DAY_TODAY


def summary_path(tenant_slug: str, *, day: str) -> str:
    """URL страницы сводки — переключатель «Сегодня / Вчера» ссылками."""
    query = f"?day={SUMMARY_DAY_YESTERDAY}" if day == SUMMARY_DAY_YESTERDAY else ""
    return f"/staff/{tenant_slug}/summary{query}"


async def build_summary_context(staff: StaffContext, *, day: str) -> dict[str, Any]:
    """Контекст шаблона `summary.html`: плитки, таблица «По службам», дата.

    Вызывается внутри контекста тенанта запроса (его ставит звено
    `TenantResolver` кабинета) — оба владельца чисел читают под RLS текущего
    тенанта, поэтому чужих чисел на странице не окажется.
    """
    zone = await checkin.hotel_zone(staff)
    today = utc_now().astimezone(zone).date()
    service_day = today if day == SUMMARY_DAY_TODAY else today - timedelta(days=1)

    summary = await requests_api.day_summary(service_day)
    escalations = await count_escalations(
        created_after=summary.day_start, created_before=summary.day_end
    )
    categories = await requests_api.list_categories()
    category_names = {category.id: category.name for category in categories}

    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "day_label": _day_label(service_day),
        "tabs": [
            {
                "label": "Сегодня",
                "href": summary_path(staff.tenant_slug, day=SUMMARY_DAY_TODAY),
                "active": day == SUMMARY_DAY_TODAY,
            },
            {
                "label": "Вчера",
                "href": summary_path(staff.tenant_slug, day=SUMMARY_DAY_YESTERDAY),
                "active": day == SUMMARY_DAY_YESTERDAY,
            },
        ],
        "is_empty": _is_empty(summary, escalations),
        "tiles": _tiles(summary, escalations),
        "service_rows": [
            {
                "name": category_names.get(row.category_id, "—"),
                "created": row.created,
                "closed": row.closed,
            }
            for row in summary.by_service
        ],
        "queue_path": f"/staff/{staff.tenant_slug}/requests",
    }


def _is_empty(summary: requests_api.RequestsDaySummary, escalations: int) -> bool:
    """Пустой день — ВСЕ числа сводки нули (§9), а не только «создано».

    День, в который закрыли четыре вчерашние заявки, пустым не является, хотя
    создано в нём ноль.
    """
    return (
        summary.created_total == 0
        and summary.closed_total == 0
        and escalations == 0
        and summary.open_now == 0
    )


def _tiles(summary: requests_api.RequestsDaySummary, escalations: int) -> list[dict[str, Any]]:
    """Плитки страницы — надписи дословно из §9 спеки.

    Два числа показываются прочерком, а не нулём: медиана, когда в этот день не
    взяли ни одной заявки, и просрочка, когда у отеля выключены напоминания.
    Ноль на их месте читался бы как «брали мгновенно» и «сроки соблюдены».
    """
    origin = summary.created_by_origin
    sources = [
        f"через бота {origin[requests_api.ServiceRequestOrigin.GUEST_CHAT]}",
        f"вручную {origin[requests_api.ServiceRequestOrigin.STAFF_MANUAL]}",
    ]
    # Третий источник — только ненулевым (issue #313): сумма подписи обязана
    # сходиться с «создано», а до первой интеграции (#122) заявок через
    # публичную дверь у отеля не будет, и «из внешней системы 0» каждый день
    # объясняло бы менеджеру то, чего в его отеле нет.
    api_created = origin[requests_api.ServiceRequestOrigin.API]
    if api_created:
        sources.append(f"из внешней системы {api_created}")

    return [
        {
            "label": "Создано",
            "value": str(summary.created_total),
            "note": " · ".join(sources) if summary.created_total else None,
        },
        {
            "label": "Закрыто",
            "value": str(summary.closed_total),
            "note": f"выполнено {summary.closed_done} · отменено {summary.closed_cancelled}"
            if summary.closed_total
            else None,
        },
        {"label": "Открыто сейчас", "value": str(summary.open_now), "note": None},
        {
            "label": "Берут в работу",
            "value": _median_label(summary.claim_median_seconds),
            "note": "половину взятых брали быстрее"
            if summary.claim_median_seconds is not None
            else "в этот день не брали",
        },
        {
            "label": "Просрочено за день",
            "value": "—" if summary.overdue is None else str(summary.overdue),
            "note": "напоминания выключены" if summary.overdue is None else None,
        },
        {"label": "Бот звал сотрудника", "value": str(escalations), "note": None},
    ]


def _median_label(seconds: int | None) -> str:
    """Медиана взятия по-русски и грубо: «40 сек», «6 мин», «2 ч» (канон
    `queue._age_label`); прочерк — если в этот день не брали."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч"


def _day_label(service_day: date) -> str:
    """Дата дня словами — «29 августа»: под переключателем «Сегодня / Вчера»
    видно, о каком именно дне числа (в 00:30 «вчера» иначе двусмысленно)."""
    return f"{service_day.day} {_MONTHS_GENITIVE[service_day.month - 1]}"
