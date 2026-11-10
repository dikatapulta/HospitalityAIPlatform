"""«Проект кодекса» и «Неточность в статье» в кабинете (spec 0072 §6, варианты (в) и (б); #898).

Пометка проекта — шапка «Кодекса» и строка на «Экзамене», пока в шапке файла кодекса
«Статус: проект»; сдача на проекте пробная — «пробно» у итога в `mentor_results.py` и
`mentor_assignments.py`. Кнопка «Неточность в статье»
— у статьи «Кодекса» и под проверкой ответа «Тренировки» и «Экзамена»: любой уровень,
только своя отметка (`mentor_flag_api.py`, `static/mentor_flags.js`). Число отметивших
по статье — раздел «Где ошибаются» (старший и менеджер); кто отметил, не видит никто.
"""

from __future__ import annotations

from typing import Any, Final

from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.humanize import plural

# Тексты — показаны основателю 10.11 файлом диспетчеру; «Экзамен» — по варианту (в), текст
# диспетчера (docs/founder-review/2026-11-10-pr925-codex-draft-flags.md).
DRAFT_BADGE: Final = "Проект кодекса"
DRAFT_CODEX_TEXT: Final = (
    "Кодекс ещё утверждается. Заметили ошибку или устаревшее правило — нажмите "
    "«Неточность в статье» под статьёй."
)
DRAFT_EXAM_TEXT: Final = (
    "Экзамен идёт по проекту кодекса — это пробная сдача: в итоги она не пойдёт, пока кодекс "
    "не утвердят. Заметили неточность — отметьте её под проверкой ответа."
)
FLAG_TEXTS: Final = {
    "off": "Неточность в статье",
    "on": "Неточность отмечена",
    "undo": "Нажмите ещё раз, чтобы снять отметку",
}
FLAGS_TITLE: Final = "Неточности в статьях"
FLAGS_HINT: Final = (
    "Статьи, у которых сотрудники нажали «Неточность в статье». Сверху — где отметок больше: "
    "их стоит проверить первыми. Новая редакция кодекса снимает отметки со статей, текст "
    "которых изменился."
)
FLAGS_EMPTY: Final = "Пока никто не отмечал неточностей."

# Вкладки, где видна кнопка: у статьи «Кодекса» и под проверкой ответа.
_FLAG_TABS: Final = frozenset({"codex", "quiz", "exam"})


async def page_context(staff: StaffContext, tab: str) -> dict[str, Any]:
    """Пометка проекта («Кодекс», «Экзамен») и кнопки отметки со своими отметками."""
    if tab not in _FLAG_TABS:
        return {}
    return {
        "codex_draft": await mentor_api.codex_is_draft(),
        "draft_badge": DRAFT_BADGE,
        "draft_text": DRAFT_CODEX_TEXT if tab == "codex" else DRAFT_EXAM_TEXT,
        "flags": {
            "endpoint": f"/staff/{staff.tenant_slug}/api/mentor/flags",
            "mine": sorted(await mentor_api.my_article_flags(staff.user_id)),
            "texts": FLAG_TEXTS,
        },
    }


def flag_rows(counts: list[mentor_api.ArticleFlagCount]) -> list[dict[str, str]]:
    """Строки раздела «Неточности в статьях»: «Статья 14. …» — «3 сотрудника»."""
    return [
        {
            "title": f"Статья {row.article_number}. {row.title}",
            "count": f"{row.count} {plural(row.count, 'сотрудник', 'сотрудника', 'сотрудников')}",
        }
        for row in counts
    ]
