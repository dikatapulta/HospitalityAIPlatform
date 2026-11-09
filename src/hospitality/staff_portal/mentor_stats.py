"""«Где ошибаются» «Наставника» — оценки «Тренировки» по главам и сотрудникам (spec 0048 §6).

По главам — старшему и менеджеру, по сотрудникам и «Обнулить» — менеджеру: право решает
`mentor.sees_names` и передаёт сюда `with_names`. Права страницы — `mentor.page_role`.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any, Final

from hospitality.modules.mentor import api as mentor_api
from hospitality.platform import staff_team
from hospitality.platform.staff_auth import StaffContext

# Строки, где ответов меньше, — внизу серым: выводы по ним делать рано («Наставник»).
MIN_ANSWERS: Final = 5


async def stats_context(staff: StaffContext, zone: tzinfo, *, with_names: bool) -> dict[str, Any]:
    """«Где ошибаются»: по главам — старшему и менеджеру, по сотрудникам — менеджеру.

    Считаются только учётки «Сотрудников» этого отеля: стёртая суперадмином
    учётка там не видна (spec 0044 §5), и её оценки не показываются нигде
    (ревью PR #512, Н-17).
    """
    members = {m.user_id: m for m in await staff_team.list_tenant_members(staff.tenant_id)}
    by_chapter = await mentor_api.quiz_stats_by_chapter(user_ids=members.keys())
    since = await mentor_api.quiz_stats_since(user_ids=members.keys())
    context: dict[str, Any] = {
        "chapter_rows": _sorted_rows(
            [(f"Глава {row.chapter_number}. {row.chapter_title}", row) for row in by_chapter]
        ),
        "user_rows": [],
        "can_reset": with_names,
        "since": f"{since.astimezone(zone):%d.%m.%Y}" if since else None,
        "answers": sum(row.total for row in by_chapter),
        "min_answers": MIN_ANSWERS,
    }
    if with_names:
        by_user = await mentor_api.quiz_stats_by_user(user_ids=members.keys())
        context["user_rows"] = _sorted_rows(
            [(members[row.user_id].display_name, row) for row in by_user]
        )
    return context


def _sorted_rows(rows: list[tuple[str, mentor_api.QuizCounts]]) -> list[dict[str, Any]]:
    """Сверху — где ошибаются чаще; строки с малым числом ответов — внизу серым."""
    views: list[dict[str, Any]] = [
        {
            "title": title,
            "share": round(100 * counts.errors / counts.total),
            "total": counts.total,
            "wrong": counts.wrong,
            "partial": counts.partial,
            "correct": counts.correct,
            "few": counts.total < MIN_ANSWERS,
        }
        for title, counts in rows
        if counts.total
    ]
    views.sort(key=lambda row: (row["few"], -row["share"], -row["total"], row["title"]))
    return views
