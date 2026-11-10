"""«Итоги экзаменов» «Наставника» — назначение экзамена и итоги по службам менеджеру
(spec 0048 §12, issue #780; назначенный экзамен — spec 0072 §3, #898).

Сверху — форма «Назначить экзамен» (тема — весь кодекс или главы, кому — всем или
службам), ниже — раздел на каждое действующее назначение, свежие сверху: тема, кому,
дата и «Снять», под ним карточки служб назначения. Итог сотрудника — итог по назначению
(`mentor_api.assigned_exam_results`: сдан однажды — сдан, иначе последняя законченная);
экзамен для себя сюда не идёт. Служба — нынешние `service_keys` (глоссарий: отдельной
сущности «служба» нет): сотрудник двух служб — в обеих, без службы — в карточке «Без
службы» (только у назначения «всем»). В сводку идут только действующие сотрудники из
«Сотрудников»: стёртая учётка там не видна (spec 0044 §5), а отключённый и с отозванным
доступом висел бы в «не проходили». Строки суперадмина нет — он не сотрудник отеля.
Права — `mentor.page_role` (только менеджер, docs/RBAC.md); действия формы —
`mentor_assign_api.py`.
"""

from __future__ import annotations

import uuid
from datetime import tzinfo
from typing import Any, Final

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import MembershipStatus, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.humanize import plural
from hospitality.staff_portal.mentor_assignments import WHOLE_CODEX, topic_label
from hospitality.staff_portal.team import service_choices

# Тексты — показаны основателю 03.11 (docs/founder-review/2026-11-03-pr784-mentor-exam-results.md)
# и 10.11 (spec 0072 §8).
TAB_TITLE: Final = "Итоги экзаменов"
NO_SERVICE_TITLE: Final = "Без службы"
NO_ATTEMPTS: Final = "нет попыток"
EVERYONE: Final = "Все сотрудники"
ASSIGN_TEXTS: Final = {
    "chapters_error": "Отметьте хотя бы одну главу.",
    "services_error": "Отметьте хотя бы одну службу.",
    "archive_confirm": (
        "Снять экзамен? Сотрудники перестанут его видеть, а его итоги пропадут с этой страницы."
    ),
    "failed": "Не получилось — обновите страницу и попробуйте ещё раз.",
}

Results = dict[tuple[uuid.UUID, uuid.UUID], mentor_api.ExamAttemptRead]
Member = staff_team.TenantMemberView


def exam_topic(attempt: mentor_api.ExamAttemptRead) -> str:
    """Тема попытки экзамена для себя строкой: глава со снимком названия или весь кодекс."""
    if attempt.chapter_number is None:
        return WHOLE_CODEX
    return f"Глава {attempt.chapter_number}. {attempt.chapter_title}"


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


def assign_hint(config: TenantConfig) -> str:
    """Подсказка формы: число вопросов и порог — настройки отеля (spec 0072 §2)."""
    return (
        f"Вопросов — {config.mentor_exam_question_count}, порог сдачи — "
        f"{config.mentor_exam_pass_percent} %: это настройки отеля."
    )


async def results_context(
    staff: StaffContext, config: TenantConfig, *, outline: list[dict[str, Any]]
) -> dict[str, Any]:
    """Форма назначения (`outline` — оглавление кодекса по частям) и разделы назначений.

    Карточки служб в разделе: ресепшен первым, затем по ключу (как галочки «Сотрудников»),
    служба, которой нет среди категорий, — ключом, «Без службы» — последней.
    """
    zone = config.tzinfo
    members = [
        member
        for member in await staff_team.list_tenant_members(staff.tenant_id)
        if member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
        and not member.is_platform_admin
    ]
    assignments = await mentor_api.list_exam_assignments()
    results = await mentor_api.assigned_exam_results(
        assignment_ids=[assignment.id for assignment in assignments],
        user_ids=[member.user_id for member in members],
    )
    choices = service_choices(await requests_api.list_categories())
    names = {choice["key"]: choice["name"] for choice in choices}
    return {
        "tab_title": TAB_TITLE,
        "assign_api": f"/staff/{staff.tenant_slug}/api/mentor/exam/assignments",
        "assign_outline": outline,
        "assign_services": choices,
        "assign_hint": assign_hint(config),
        "assign_texts": ASSIGN_TEXTS,
        "sections": [
            _section(assignment, members, results, choices, names, zone)
            for assignment in assignments
        ],
    }


def _section(
    assignment: mentor_api.ExamAssignmentRead,
    members: list[Member],
    results: Results,
    choices: list[dict[str, str]],
    names: dict[str, str],
    zone: tzinfo,
) -> dict[str, Any]:
    by_service: dict[str | None, list[Member]] = {}
    for member in members:
        member_keys: list[str | None] = [*member.service_keys] or [None]
        for key in member_keys:
            by_service.setdefault(key, []).append(member)
    # Назначено службам — карточка у каждой из них, даже пустая; всем — у каждой, где есть люди.
    shown = set(assignment.service_keys) or {key for key in by_service if key is not None}
    keys = [choice["key"] for choice in choices if choice["key"] in shown]
    keys += sorted(shown - names.keys())
    cards = [
        _service_card(names.get(key, key), by_service.get(key, []), assignment.id, results, zone)
        for key in keys
    ]
    if not assignment.service_keys and None in by_service:
        cards.append(
            _service_card(NO_SERVICE_TITLE, by_service[None], assignment.id, results, zone)
        )
    title, chapters = topic_label(assignment)
    audience = (
        ", ".join(names.get(key, key) for key in assignment.service_keys)
        if assignment.service_keys
        else EVERYONE
    )
    return {
        "id": str(assignment.id),
        "topic": title,
        "chapters": chapters,
        "meta": [audience, f"назначен {assignment.created_at.astimezone(zone):%d.%m.%Y}"],
        "services": cards,
    }


def _service_card(
    title: str,
    members: list[Member],
    assignment_id: uuid.UUID,
    results: Results,
    zone: tzinfo,
) -> dict[str, Any]:
    taken = [
        results[assignment_id, member.user_id]
        for member in members
        if (assignment_id, member.user_id) in results
    ]
    passed = sum(1 for attempt in taken if attempt.passed)
    average = average_percent([attempt.percent for attempt in taken if attempt.percent is not None])
    rows = [
        _member_row(member.display_name, results.get((assignment_id, member.user_id)), zone)
        for member in members
    ]
    rows.sort(key=lambda row: row["name"].casefold())
    return {
        "title": title,
        "members": len(members),
        "passed": passed,
        "summary": summary_parts(
            members=len(members), passed=passed, failed=len(taken) - passed, average=average
        ),
        "rows": rows,
    }


def _member_row(
    name: str, attempt: mentor_api.ExamAttemptRead | None, zone: tzinfo
) -> dict[str, Any]:
    if attempt is None or attempt.finished_at is None:
        return {"name": name, "result": NO_ATTEMPTS, "passed": None, "when": "—"}
    return {
        "name": name,
        "result": f"{'сдан' if attempt.passed else 'не сдан'} — {attempt.percent} %",
        "passed": attempt.passed,
        "when": f"{attempt.finished_at.astimezone(zone):%d.%m.%Y}",
    }
