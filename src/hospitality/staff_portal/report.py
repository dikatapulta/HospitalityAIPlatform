"""Лист «Отчёт» (spec 0083, issue #950): неделя или месяц одним листом для руководства.

Черновик analytics v1 без модуля `analytics/`: каждое число считает владелец данных,
страница их только раскладывает (канон `summary.py`, spec 0035 §6). Заявки —
`requests_api.period_summary`, оценки — `feedback_api.count_feedback`, экзамен —
`mentor_results.results_context` (те же сотрудники и слова, что в «Итогах экзаменов»),
идеи — `mentor_api.count_sent_ideas`, вопросы без ответа — `channels/common`. Окно —
одно на лист: его отдаёт `period_summary`, остальные считают за него же.

Период — прошлая неделя, прошлый месяц или свои даты (§2.2), разбор — `report_period.py`.
Только менеджер, выключатель `ENABLE_MANAGER_REPORT` (§2.5). Модели нет.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hospitality.channels.common.unanswered_questions import count_unanswered_questions
from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.humanize import duration_label_ru, month_day_ru
from hospitality.staff_portal import ideas, mentor, mentor_results, queue
from hospitality.staff_portal.report_period import (
    PERIOD_CUSTOM,
    PERIOD_TITLES,
    parse_report_period,
    range_label,
    report_path,
)

DASH: Final = "—"
# Колонки таблицы «По службам» (§4); плитки — те же числа без «Отменено».
COLUMNS: Final = ("Создано", "Выполнено", "Отменено", "Просрочено", "Взяли за", "Выполнили за")
TILE_LABELS: Final = ("Создано", "Выполнено", "Просрочено", "Взяли за", "Выполнили за")
UNANSWERED_RETENTION_NOTE: Final = "вопросы хранятся {days} дней"
IDEAS_RETENTION_NOTE: Final = "идеи хранятся {days} дней"


def section_enabled() -> bool:
    """`ENABLE_MANAGER_REPORT` (spec 0083 §2.5): одна проверка на меню и страницу."""
    return get_settings().enable_manager_report


async def build_report_context(staff: StaffContext, params: Mapping[str, str]) -> dict[str, Any]:
    """Контекст `report.html`. Вызывается внутри контекста тенанта запроса: все
    владельцы чисел читают под RLS текущего тенанта (P-4)."""
    config = await queue.tenant_config(staff)
    zone = config.tzinfo if config is not None else UTC
    now = utc_now()
    period = parse_report_period(params, today=now.astimezone(zone).date())
    current = await requests_api.period_summary(period.first_day, period.last_day)
    previous = await requests_api.period_summary(period.previous_first, period.previous_last)
    names = {category.id: category.name for category in await requests_api.list_categories()}
    reminders_off = current.total.overdue is None
    return {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "tabs": [
            {
                "label": label,
                "href": report_path(staff.tenant_slug, key),
                "active": key == period.key,
            }
            for key, label in PERIOD_TITLES.items()
            if key != PERIOD_CUSTOM
        ],
        "custom_active": period.key == PERIOD_CUSTOM,
        "custom_title": PERIOD_TITLES[PERIOD_CUSTOM],
        "form": {
            "action": report_path(staff.tenant_slug, PERIOD_CUSTOM),
            "first": period.first_day.isoformat(),
            "last": period.last_day.isoformat(),
            "max": now.astimezone(zone).date().isoformat(),
        },
        "error": period.error,
        "range_label": range_label(period.first_day, period.last_day),
        "previous_label": range_label(period.previous_first, period.previous_last),
        "columns": COLUMNS,
        "is_empty": _is_empty(current.total),
        "tiles": _tiles(current.total, previous.total),
        "service_rows": _service_rows(current, previous, names),
        "reminders_off": reminders_off,
        "feedback": await _feedback(config, current, previous, names),
        "exam": await _exam(staff, config, day=month_day_ru(now.astimezone(zone).date())),
        "ideas": await _ideas(config, current, previous, now),
        "unanswered": await _unanswered(current, previous, now),
        "knowledge_path": f"/staff/{staff.tenant_slug}/knowledge",
    }


def _is_empty(total: requests_api.PeriodCounts) -> bool:
    return total.created == 0 and total.done == 0 and total.cancelled == 0


def _count(value: int | None) -> str:
    return DASH if value is None else str(value)


def _duration(seconds: int | None) -> str:
    """Медиана словами, как в сводке дня; мерить нечего — прочерк, а не «0 сек»."""
    return DASH if seconds is None else duration_label_ru(seconds)


def _cells(counts: requests_api.PeriodCounts) -> list[str]:
    """Числа строки в порядке колонок таблицы «По службам» (§4)."""
    return [
        str(counts.created),
        str(counts.done),
        str(counts.cancelled),
        _count(counts.overdue),
        _duration(counts.claim_median_seconds),
        _duration(counts.done_median_seconds),
    ]


def _tiles(
    current: requests_api.PeriodCounts, previous: requests_api.PeriodCounts
) -> list[dict[str, str]]:
    """Плитки отеля (§4): число периода и «было N» прошлого."""
    pairs = dict(zip(COLUMNS, zip(_cells(current), _cells(previous), strict=True), strict=True))
    return [
        {"label": label, "value": pairs[label][0], "previous": f"было {pairs[label][1]}"}
        for label in TILE_LABELS
    ]


def _service_rows(
    current: requests_api.RequestsPeriodSummary,
    previous: requests_api.RequestsPeriodSummary,
    names: Mapping[uuid.UUID, str],
) -> list[dict[str, Any]]:
    """Строка на службу, где что-то было в периоде или в прошлом; по убыванию
    созданных в периоде, затем по имени (§2.3). Нет службы в окне — нули и «—»."""
    now_rows = {row.category_id: row for row in current.by_service}
    before_rows = {row.category_id: row for row in previous.by_service}
    empty = requests_api.PeriodCounts()
    ordered = sorted(
        now_rows.keys() | before_rows.keys(),
        key=lambda key: (-now_rows.get(key, empty).created, names.get(key, DASH).casefold()),
    )
    return [
        {
            "name": names.get(category_id, DASH),
            "cells": list(
                zip(
                    _cells(now_rows.get(category_id, empty)),
                    _cells(before_rows.get(category_id, empty)),
                    strict=True,
                )
            ),
        }
        for category_id in ordered
    ]


async def _feedback(
    config: TenantConfig | None,
    current: requests_api.RequestsPeriodSummary,
    previous: requests_api.RequestsPeriodSummary,
    names: Mapping[uuid.UUID, str],
) -> dict[str, Any] | None:
    """«Оценки гостей» (§2.3 п. 2) — только при включённых оценках отеля."""
    if config is None or not config.guest_feedback_enabled:
        return None
    now_counts = await feedback_api.count_feedback(
        start=current.period_start, end=current.period_end
    )
    before_counts = await feedback_api.count_feedback(
        start=previous.period_start, end=previous.period_end
    )
    now_by = {count.category_id: count for count in now_counts}
    before_by = {count.category_id: count for count in before_counts}
    ordered = sorted(
        now_by.keys() | before_by.keys(),
        key=lambda key: (-_votes(now_by.get(key)), names.get(key, DASH).casefold()),
    )
    rows = [
        {
            "name": names.get(category_id, DASH),
            "value": _ratings(now_by.get(category_id)),
            "previous": _ratings(before_by.get(category_id)),
        }
        for category_id in ordered
    ]
    return {
        "value": _ratings_total(now_counts),
        "previous": _ratings_total(before_counts),
        "rows": rows,
    }


def _votes(count: feedback_api.FeedbackCount | None) -> int:
    return 0 if count is None else count.up + count.down


def _ratings(count: feedback_api.FeedbackCount | None) -> str:
    up, down = (0, 0) if count is None else (count.up, count.down)
    return f"хорошо {up} · плохо {down}"


def _ratings_total(counts: Sequence[feedback_api.FeedbackCount]) -> str:
    return f"хорошо {sum(c.up for c in counts)} · плохо {sum(c.down for c in counts)}"


async def _exam(
    staff: StaffContext, config: TenantConfig | None, *, day: str
) -> dict[str, Any] | None:
    """«Экзамен „Наставника“» на сегодня (§2.3 п. 3): разделы «Итогов экзаменов»
    без строк сотрудников. Раздела у отеля нет или нет назначений — блока нет."""
    if config is None or not mentor.section_on(config):
        return None
    results = await mentor_results.results_context(staff, config, outline=[])
    if not results["sections"]:
        return None
    return {
        "day": day,
        "sections": [
            {
                "topic": section["topic"],
                "meta": section["meta"],
                "services": [
                    {"title": card["title"], "summary": card["summary"]}
                    for card in section["services"]
                ],
            }
            for section in results["sections"]
        ],
        "notes": [
            note for note in (results.get("draft_note"), results.get("consent_note")) if note
        ],
    }


def _kept(period: requests_api.RequestsPeriodSummary, now: datetime, days: int) -> bool:
    """Строки окна ещё живы: начало окна моложе срока хранения (§2.3 п. 5)."""
    return period.period_start >= now - timedelta(days=days)


async def _ideas(
    config: TenantConfig | None,
    current: requests_api.RequestsPeriodSummary,
    previous: requests_api.RequestsPeriodSummary,
    now: datetime,
) -> dict[str, str] | None:
    """«Идеи сотрудников» (§2.3 п. 4) — только при включённых идеях отеля; число
    по отелю, без служб и имён."""
    if not ideas.section_on(config):
        return None
    days = mentor_api.IDEA_RETENTION_DAYS

    async def count(period: requests_api.RequestsPeriodSummary) -> str:
        if not _kept(period, now, days):
            return DASH
        return str(
            await mentor_api.count_sent_ideas(
                sent_after=period.period_start, sent_before=period.period_end
            )
        )

    return {
        "value": await count(current),
        "previous": await count(previous),
        "note": IDEAS_RETENTION_NOTE.format(days=days),
    }


async def _unanswered(
    current: requests_api.RequestsPeriodSummary,
    previous: requests_api.RequestsPeriodSummary,
    now: datetime,
) -> dict[str, str]:
    """«Вопросы гостей без ответа» (§2.3 п. 5); окно старше срока хранения — «—»."""
    days = get_settings().messages_retention_days

    async def count(period: requests_api.RequestsPeriodSummary) -> str:
        if not _kept(period, now, days):
            return DASH
        return str(
            await count_unanswered_questions(
                created_after=period.period_start, created_before=period.period_end
            )
        )

    return {
        "value": await count(current),
        "previous": await count(previous),
        "note": UNANSWERED_RETENTION_NOTE.format(days=days),
    }
