"""Назначенный экзамен «Наставника» в кабинете — то, что видит сотрудник (spec 0072 §3, §8).

Строка над вкладками на всех страницах раздела — назначенные и ещё не сданные; на «Экзамене»
сверху — «Назначены вам» с состоянием и «Начать экзамен» (у сданного кнопки нет). Назначение
сотруднику — действующее «всем» или его нынешней службе (`StaffContext.service_keys`).
Менеджерская сторона — форма и итоги по назначению — `mentor_results.py`.
"""

from __future__ import annotations

import uuid
from datetime import tzinfo
from typing import Any, Final

from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.humanize import plural

# Тексты — spec 0072 §8 (показаны основателю 10.11, его ответ — в #898).
WHOLE_CODEX: Final = "Весь кодекс"
ASSIGNED_TITLE: Final = "Назначены вам"
NOT_TAKEN: Final = "Ещё не сдавали"
OWN_TITLE: Final = "Экзамен для себя"
OWN_HINT: Final = "Любая тема, для тренировки — в итоги для менеджера не идёт."
UNAVAILABLE_TEXT: Final = "Этот экзамен сейчас не пройти — обновите страницу или скажите менеджеру."
BANNER_LINK: Final = "К экзамену"
# Пробная сдача на проекте кодекса (spec 0072 §6, вариант (в)) — в итог не идёт.
TRIAL_STATE: Final = "Пробно — {percent} %: сдача по проекту кодекса в итог не идёт"
TRIAL_MARK: Final = "пробно"


def topic_label(assignment: mentor_api.ExamAssignmentRead) -> tuple[str, str | None]:
    """Тема строкой и, у нескольких глав, их названия второй строкой:
    «Весь кодекс», «Глава 12. Горничная», «Главы 3, 12, 14» + «Портье · Горничная · …»."""
    chapters = assignment.chapters
    if not chapters:
        return WHOLE_CODEX, None
    if len(chapters) == 1:
        return f"Глава {chapters[0].number}. {chapters[0].title}", None
    numbers = ", ".join(str(chapter.number) for chapter in chapters)
    return f"Главы {numbers}", " · ".join(chapter.title for chapter in chapters)


def state_text(attempt: mentor_api.ExamAttemptRead | None, zone: tzinfo) -> str:
    """Состояние назначения у сотрудника — по итогу `assigned_exam_results`."""
    if attempt is None or attempt.finished_at is None:
        return NOT_TAKEN
    if attempt.on_draft_codex:
        return TRIAL_STATE.format(percent=attempt.percent)
    if attempt.passed:
        return f"Сдан {attempt.finished_at.astimezone(zone):%d.%m.%Y} — {attempt.percent} %"
    return f"Не сдан — {attempt.percent} %, нужно {attempt.pass_percent} %"


async def assigned_to_me(staff: StaffContext, zone: tzinfo) -> list[dict[str, Any]]:
    """«Назначены вам»: действующие назначения сотрудника, свежие сверху, с его итогом."""
    mine = [
        assignment
        for assignment in await mentor_api.list_exam_assignments()
        if assignment.is_for(staff.service_keys)
    ]
    results = await mentor_api.assigned_exam_results(
        assignment_ids=[assignment.id for assignment in mine], user_ids=[staff.user_id]
    )
    items = []
    for assignment in mine:
        attempt = results.get((assignment.id, staff.user_id))
        title, chapters = topic_label(assignment)
        items.append(
            {
                "id": str(assignment.id),
                "topic": title,
                "chapters": chapters,
                "state": state_text(attempt, zone),
                "passed": None if attempt is None else attempt.passes_assignment,
            }
        )
    return items


def banner(items: list[dict[str, Any]], href: str) -> dict[str, str] | None:
    """Строка над вкладками — назначенные и ещё не сданные; всё сдано — строки нет."""
    open_items = [item for item in items if not item["passed"]]
    if not open_items:
        return None
    if len(open_items) == 1:
        text = f"Вам назначен экзамен: {open_items[0]['topic']}"
    else:
        count = len(open_items)
        text = f"Вам назначено {count} {plural(count, 'экзамен', 'экзамена', 'экзаменов')}"
    return {"text": text, "link": BANNER_LINK, "href": href}


async def attempt_topics(attempts: list[mentor_api.ExamAttemptRead]) -> dict[uuid.UUID, str]:
    """Тема назначенных попыток для «Моих попыток» — по назначению, снятому тоже."""
    ids = {attempt.assignment_id for attempt in attempts if attempt.assignment_id is not None}
    assignments = await mentor_api.get_exam_assignments(ids)
    return {key: topic_label(value)[0] for key, value in assignments.items()}
