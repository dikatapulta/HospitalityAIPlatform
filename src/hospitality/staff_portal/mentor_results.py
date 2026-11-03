"""«Итоги экзаменов» «Наставника» — сводка по службам менеджеру (spec 0048 §12, issue #780).

Итог сотрудника — его последний законченный экзамен (`mentor_api.latest_exam_results`),
служба — нынешние `service_keys` (глоссарий: отдельной сущности «служба» нет): сотрудник
двух служб — в обеих, без службы — в карточке «Без службы». В сводку идут только
действующие сотрудники из «Сотрудников»: стёртая учётка там не видна (spec 0044 §5), а
отключённый и с отозванным доступом висел бы в «не проходили». Строки суперадмина нет —
он не сотрудник отеля. Права — `mentor.page_role` (только менеджер, docs/RBAC.md).
"""

from __future__ import annotations

import uuid
from datetime import tzinfo
from typing import Any, Final

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.models import MembershipStatus, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.humanize import plural
from hospitality.staff_portal.team import service_choices

# Тексты — показаны основателю 03.11 (docs/founder-review/2026-11-03-pr784-mentor-exam-results.md).
TAB_TITLE: Final = "Итоги экзаменов"
NO_SERVICE_TITLE: Final = "Без службы"
WHOLE_CODEX: Final = "Весь кодекс"
NO_ATTEMPTS: Final = "нет попыток"


def average_percent(percents: list[int]) -> int | None:
    """Средний процент вниз до целого, как процент самого экзамена; сдававших нет — `None`."""
    return sum(percents) // len(percents) if percents else None


def summary_parts(*, members: int, passed: int, failed: int, average: int | None) -> list[str]:
    """Части строки карточки: «12 сотрудников · сдали 7 · не сдали 3 · не проходили 2 ·
    в среднем 76 %» — шаблон ставит точки и не рвёт часть переносом.

    Нулевые части не пишутся; «не проходили», а не «не сдавали»: рядом с «не сдали»
    их не отличить.
    """
    parts = [f"{members} {plural(members, 'сотрудник', 'сотрудника', 'сотрудников')}"]
    untested = members - passed - failed
    for count, label in ((passed, "сдали"), (failed, "не сдали"), (untested, "не проходили")):
        if count:
            parts.append(f"{label} {count}")
    if average is not None:
        parts.append(f"в среднем {average} %")
    return parts


async def results_context(staff: StaffContext, zone: tzinfo) -> dict[str, Any]:
    """Карточки служб: ресепшен первым, затем по ключу (как галочки «Сотрудников»),
    служба, которой нет среди категорий, — ключом, «Без службы» — последней."""
    members = [
        member
        for member in await staff_team.list_tenant_members(staff.tenant_id)
        if member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
        and not member.is_platform_admin
    ]
    results = {
        attempt.user_id: attempt
        for attempt in await mentor_api.latest_exam_results(
            user_ids=[member.user_id for member in members]
        )
    }
    by_service: dict[str | None, list[staff_team.TenantMemberView]] = {}
    for member in members:
        keys: list[str | None] = [*member.service_keys] or [None]
        for key in keys:
            by_service.setdefault(key, []).append(member)
    choices = service_choices(await requests_api.list_categories())
    names = {choice["key"]: choice["name"] for choice in choices}
    known = [choice["key"] for choice in choices if choice["key"] in by_service]
    unknown = sorted(key for key in by_service if key is not None and key not in names)
    services = [
        _service_card(names.get(key, key), by_service[key], results, zone)
        for key in [*known, *unknown]
    ]
    if None in by_service:
        services.append(_service_card(NO_SERVICE_TITLE, by_service[None], results, zone))
    return {"services": services, "has_results": bool(results), "tab_title": TAB_TITLE}


def _service_card(
    title: str,
    members: list[staff_team.TenantMemberView],
    results: dict[uuid.UUID, mentor_api.ExamAttemptRead],
    zone: tzinfo,
) -> dict[str, Any]:
    taken = [results[member.user_id] for member in members if member.user_id in results]
    passed = sum(1 for attempt in taken if attempt.passed)
    average = average_percent([attempt.percent for attempt in taken if attempt.percent is not None])
    rows = [
        _member_row(member.display_name, results.get(member.user_id), zone) for member in members
    ]
    rows.sort(key=lambda row: row["name"].casefold())
    return {
        "title": title,
        "summary": summary_parts(
            members=len(members), passed=passed, failed=len(taken) - passed, average=average
        ),
        "rows": rows,
    }


def _member_row(
    name: str, attempt: mentor_api.ExamAttemptRead | None, zone: tzinfo
) -> dict[str, Any]:
    if attempt is None or attempt.finished_at is None:
        return {"name": name, "topic": None, "result": NO_ATTEMPTS, "passed": None, "when": "—"}
    topic = (
        WHOLE_CODEX
        if attempt.chapter_number is None
        else f"Глава {attempt.chapter_number}. {attempt.chapter_title}"
    )
    return {
        "name": name,
        "topic": topic,
        "result": f"{attempt.percent} %, {'сдан' if attempt.passed else 'не сдан'}",
        "passed": attempt.passed,
        "when": f"{attempt.finished_at.astimezone(zone):%d.%m.%Y}",
    }
