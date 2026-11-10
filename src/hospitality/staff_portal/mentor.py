"""Раздел «Наставник» кабинета (spec 0048 §2, §6–7; issue #492, PR 4 серии).

Вкладки: «Спросить», «Тренировка» и «Экзамен» — чат с потоком ответа
(`mentor_api.py`, `static/mentor_chat.js`), «Кодекс» — документ целиком с
поиском и фильтром «Для кого» (`static/mentor_codex.js`), «Где ошибаются» —
статистика «Тренировки» (`mentor_stats.py`), «Итоги экзаменов» — назначение экзамена и
итоги по службам (`mentor_results.py`); назначенный экзамен у сотрудника — строка над
вкладками и «Назначены вам» (`mentor_assignments.py`, spec 0072 §3). Данные — модуль
`mentor`, ответы модели — `ai/mentor`; здесь — права, контекст страниц и тексты сотруднику
по кодам ошибок (§7).

Права (§6, docs/RBAC.md) — только `require_role` (P-12): вкладки чата и кодекса —
любой уровень, «Где ошибаются» — старший и менеджер, имена в ней, «Обнулить»,
загрузка кодекса и «Итоги экзаменов» — менеджер. Диалог сотрудника читает только
он сам: каждый вызов модуля — с `user_id` вошедшего. Раздел выключен у отеля
(`TenantConfig.mentor_enabled`, умолчание `false`) — пункта меню нет, страницы и
действия отвечают 404, как неизвестный адрес.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import tzinfo
from typing import Any, Final

from fastapi import HTTPException, Request

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import MAX_MESSAGE_CHARS
from hospitality.ai.mentor.exam import ExamProgress
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform import staff_auth
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import AppError
from hospitality.shared.humanize import plural
from hospitality.staff_portal import mentor_assignments, mentor_results, mentor_stats, queue

ConversationMode = mentor_api.ConversationMode

# Вкладки в порядке полосы; «Где ошибаются» — старшему и менеджеру, итоги — менеджеру.
TABS: Final = {
    "ask": "Спросить",
    "quiz": "Тренировка",
    "exam": "Экзамен",
    "codex": "Кодекс",
    "stats": "Где ошибаются",
    "results": mentor_results.TAB_TITLE,
}
_TEMPLATES: Final = {
    "ask": "mentor_chat.html",
    "quiz": "mentor_chat.html",
    "exam": "mentor_chat.html",
    "codex": "mentor_codex.html",
    "stats": "mentor_stats.html",
    "results": "mentor_results.html",
}
# Шапка раздела и пустой «Спросить» (#888; показаны основателю 08.11 в чате окна).
PAGE_SUB: Final = "Отвечает по кодексу отеля"
ASK_PLACEHOLDER: Final = "Спросите о стандартах отеля"
SUGGESTION_COUNT: Final = 4
SUGGESTION_TEMPLATE: Final = "{title} — как действовать?"

# Тексты сотруднику по кодам (spec 0048 §7; новые — FOUNDER_REVIEW_QUEUE 28.10).
NO_CODEX_TEXT: Final = "Кодекс ещё не загружен — попросите менеджера."
BUDGET_TEXT: Final = (
    "Наставник на сегодня ответил на все вопросы, что мог. "
    "Попробуйте завтра или спросите руководителя смены."
)
UNAVAILABLE_TEXT: Final = "Наставник сейчас не отвечает. Попробуйте через минуту."
NO_QUESTION_TEXT: Final = "Этот вопрос уже проверен — нажмите «Новый вопрос»."
CUT_TEXT: Final = "Ответ оборвался — задайте вопрос ещё раз."
QUESTION_LABEL: Final = "Вопрос для тренировки"
NO_EXAM_QUESTION_TEXT: Final = "Этот вопрос экзамена уже не открыт — обновите страницу."

# Экзамен (spec 0048 §12; показаны основателю 03.11 — docs/founder-review/). Тексты хода
# собирает static/mentor_exam.js: {n}, {total}, {percent}… — поля хода попытки.
EXAM_TITLE: Final = "Экзамен по кодексу"
EXAM_START: Final = "Начать экзамен"
EXAM_TEXTS: Final = {
    "label": "Вопрос {n} из {total}",
    "restart": "Начать экзамен заново? Эта попытка не засчитается.",
    "not_counted": (
        "Оценка не разобралась — этот ответ не засчитан. Следующий вопрос — вместо него."
    ),
    "passed": "Экзамен сдан — {percent} %",
    "failed": "Экзамен не сдан — {percent} %",
    "score": (
        "Верно — {correct}, частично — {partial}, неверно — {wrong}. "
        "Порог сдачи — {pass_percent} %."
    ),
    "open": "Ваш ответ…",
    # Полоса хода над лентой и кнопка после итога (#888; показаны основателю 08.11).
    "progress": "Вопрос {n} из {total}",
    "tally": "верно {correct} · частично {partial} · неверно {wrong}",
    "again": "Пройти ещё раз",
    "unavailable_assigned": mentor_assignments.UNAVAILABLE_TEXT,
}


def exam_intro(question_count: int, pass_percent: int) -> str:
    """Как устроен экзамен — числа из настроек отеля (`TenantConfig`, NG-7)."""
    questions = plural(question_count, "вопрос", "вопроса", "вопросов")
    return (
        f"{question_count} {questions} подряд — как в тренировке, но с итогом. "
        "Верный ответ — 1 балл, частично верный — половина. "
        f"Экзамен сдан, если набрано {pass_percent} % и больше."
    )


any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
_senior_role: Final = staff_auth.require_role(StaffRole.SENIOR, StaffRole.MANAGER)
manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


def section_on(config: TenantConfig | None) -> bool:
    """Раздел включён у отеля (флаг тенанта, spec 0048 §2); отель без конфига — нет."""
    return config is not None and config.mentor_enabled


async def require_section(staff: StaffContext) -> TenantConfig:
    """Конфиг отеля с включённым разделом; выключен — 404, как неизвестный адрес."""
    config = await queue.tenant_config(staff)
    if config is None or not section_on(config):
        raise HTTPException(status_code=404)
    return config


def page_role(tab: str) -> Callable[[Request], Awaitable[StaffContext]]:
    """Зависимость прав вкладки (§6): «Где ошибаются» — старшему и менеджеру,
    «Итоги экзаменов» — менеджеру (§12, имена и результаты всех служб)."""
    if tab == "results":
        return manager_role
    return _senior_role if tab == "stats" else any_role


def _tab_visible(key: str, role: StaffRole) -> bool:
    """Вкладка в полосе — только тем, кому её страница открыта (`page_role`)."""
    if key == "results":
        return role is StaffRole.MANAGER
    return key != "stats" or role is not StaffRole.STAFF


def sees_names(staff: StaffContext) -> bool:
    """Имена в «Где ошибаются», «Обнулить» и загрузка кодекса — только менеджеру (§6)."""
    return staff.role_key is StaffRole.MANAGER


def error_text(error: AppError) -> str:
    """Текст сотруднику по коду ошибки хода (§7); разметка кодекса — как есть."""
    if error.code == mentor_api.ERR_MENTOR_CODEX_MISSING:
        return NO_CODEX_TEXT
    if error.code == mentor_api.ERR_MENTOR_NO_QUIZ_QUESTION:
        return NO_QUESTION_TEXT
    if error.code == mentor_api.ERR_MENTOR_NO_EXAM_QUESTION:
        return NO_EXAM_QUESTION_TEXT
    if error.code == mentor_api.ERR_MENTOR_CODEX_MARKUP:
        return error.message
    if error.code == gateway.ERR_AI_BUDGET_EXCEEDED:
        return BUDGET_TEXT
    return UNAVAILABLE_TEXT


def base_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/mentor"


async def build_page(staff: StaffContext, tab: str) -> tuple[str, dict[str, Any]]:
    """Шаблон и контекст вкладки. Раздел выключен или вкладки нет — 404."""
    if tab not in TABS:
        raise HTTPException(status_code=404)
    config = await require_section(staff)
    path = base_path(staff.tenant_slug)
    assigned = await mentor_assignments.assigned_to_me(staff, config.tzinfo)
    context: dict[str, Any] = {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "tab": tab,
        "tabs": [
            {"key": key, "title": title, "href": path if key == "ask" else f"{path}/{key}"}
            for key, title in TABS.items()
            if _tab_visible(key, staff.role_key)
        ],
        "codex_href": f"{path}/codex",
        "api": f"/staff/{staff.tenant_slug}/api/mentor",
        "no_codex_text": NO_CODEX_TEXT,
        "page_sub": PAGE_SUB,
        "exam_banner": mentor_assignments.banner(assigned, f"{path}/exam"),
    }
    if tab in ("ask", "quiz"):
        context.update(await _chat_context(staff, ConversationMode(tab), path))
    elif tab == "exam":
        context.update(await _exam_context(staff, config, assigned))
    elif tab == "codex":
        context.update(await _codex_context(staff, config.tzinfo))
    elif tab == "results":
        codex = await mentor_api.load_codex_document()
        outline = _topic_groups(codex) if codex else []
        context.update(await mentor_results.results_context(staff, config, outline=outline))
    else:
        context.update(
            await mentor_stats.stats_context(staff, config.tzinfo, with_names=sees_names(staff))
        )
    return _TEMPLATES[tab], context


async def _chat_context(
    staff: StaffContext, mode: mentor_api.ConversationMode, path: str
) -> dict[str, Any]:
    """Лента диалога (вся, вместе с пересказанным в резюме) и источники под ответами.

    Под ответом — статьи, на которые он сослался и которые есть в нынешнем
    кодексе: после перезагрузки кодекса пропавшая статья из-под ответа уходит.
    Вопрос «Тренировки» — ответ наставника без ссылок: источник выдал бы ответ.
    """
    has_codex = await mentor_api.has_codex()
    conversation = await mentor_api.get_conversation(staff.user_id, mode)
    messages = conversation.messages if conversation else []
    cited = sorted({number for m in messages for number in m.cited_articles})
    articles = await mentor_api.get_articles(cited)
    quiz = mode is ConversationMode.QUIZ
    codex = await mentor_api.load_codex_document() if has_codex else None
    score = None
    if quiz:
        mine = await mentor_api.quiz_stats_by_user(user_ids=[staff.user_id])
        score = mine[0] if mine else None
    return {
        "mode": mode.value,
        "has_codex": has_codex,
        "max_chars": MAX_MESSAGE_CHARS,
        "cut_text": CUT_TEXT,
        "unavailable_text": UNAVAILABLE_TEXT,
        "question_label": QUESTION_LABEL,
        "ask_placeholder": ASK_PLACEHOLDER,
        "quiz_open": bool(conversation and conversation.quiz_question),
        "topic_groups": _topic_groups(codex) if quiz and codex else [],
        "suggestions": suggestions(codex) if mode is ConversationMode.ASK else [],
        "score": {
            "total": score.total if score else 0,
            "correct": score.correct if score else 0,
            "partial": score.partial if score else 0,
            "wrong": score.wrong if score else 0,
        },
        "messages": [
            {
                "role": "user" if m.role is mentor_api.MessageRole.USER else "bot",
                "text": m.text,
                "label": QUESTION_LABEL if quiz and _is_question(m) else None,
                "sources": [
                    {
                        "number": articles[n].number,
                        "title": articles[n].title,
                        "href": f"{path}/codex#{articles[n].anchor}",
                    }
                    for n in m.cited_articles
                    if n in articles
                ],
            }
            for m in messages
        ],
    }


def suggestions(codex: mentor_api.Codex | None) -> list[str]:
    """Подсказки пустого «Спросить»: вопросы по заголовкам случайных статей.

    Статьи — из глав для должностей (части после первой, как фильтр «Для кого»):
    общая часть — о самом кодексе. Других частей нет — из всего кодекса. Порядок —
    как в кодексе, чтобы подсказки читались списком, а не вразброс.
    """
    if codex is None:
        return []
    every = [article for _, _, article in codex.walk()]
    general = codex.parts[0] if codex.parts else None
    pool = [article for part, _, article in codex.walk() if part is not general] or every
    picked = random.sample(pool, min(SUGGESTION_COUNT, len(pool)))
    picked.sort(key=every.index)
    return [SUGGESTION_TEMPLATE.format(title=article.title) for article in picked]


async def _exam_context(
    staff: StaffContext, config: TenantConfig, assigned: list[dict[str, Any]]
) -> dict[str, Any]:
    """«Экзамен» (§12): «Назначены вам» (spec 0072 §3), темы экзамена для себя, идущая
    попытка, свои законченные попытки (у назначенной — тема назначения).

    Текстов прошлых вопросов и проверок нет — их не хранят: идущая попытка
    показывает открытый вопрос, а без него — кнопку «Следующий вопрос» (JS).
    Брошенные попытки в списке не показываются.
    """
    has_codex = await mentor_api.has_codex()
    codex = await mentor_api.load_codex_document() if has_codex else None
    attempt = await mentor_api.current_exam(staff.user_id)
    finished = await mentor_api.list_exam_attempts(staff.user_id)
    messages: list[dict[str, Any]] = []
    if attempt is not None and attempt.current_question is not None:
        label = EXAM_TEXTS["label"].format(n=attempt.answered + 1, total=attempt.question_count)
        messages.append(
            {"role": "bot", "text": attempt.current_question, "label": label, "sources": []}
        )
    zone = config.tzinfo
    topics = await mentor_assignments.attempt_topics([*finished, *([attempt] if attempt else [])])
    attempts = [
        {
            "when": f"{a.finished_at.astimezone(zone):%d.%m.%Y %H:%M}",
            "percent": a.percent,
            "passed": a.passed,
            "topic": _attempt_topic(a, topics),
        }
        for a in finished
        if a.finished_at is not None
    ]
    return {
        "mode": "exam",
        "has_codex": has_codex,
        "max_chars": MAX_MESSAGE_CHARS,
        "cut_text": CUT_TEXT,
        "unavailable_text": UNAVAILABLE_TEXT,
        "question_label": "",
        "quiz_open": bool(messages),
        "topic_groups": _topic_groups(codex) if codex else [],
        "messages": messages,
        "exam_title": EXAM_TITLE,
        "exam_intro": exam_intro(
            config.mentor_exam_question_count, config.mentor_exam_pass_percent
        ),
        "exam_start": EXAM_START,
        "exam_texts": EXAM_TEXTS,
        "exam_topic": _attempt_topic(attempt, topics) if attempt is not None else None,
        "assigned": assigned,
        "assigned_title": mentor_assignments.ASSIGNED_TITLE,
        "own_title": mentor_assignments.OWN_TITLE,
        "own_hint": mentor_assignments.OWN_HINT,
        "progress_label": (
            EXAM_TEXTS["progress"].format(
                n=min(attempt.answered + 1, attempt.question_count), total=attempt.question_count
            )
            if attempt is not None
            else ""
        ),
        "progress_tally": (
            EXAM_TEXTS["tally"].format(
                correct=attempt.correct_count,
                partial=attempt.partial_count,
                wrong=attempt.wrong_count,
            )
            if attempt is not None
            else ""
        ),
        "exam_progress": (
            ExamProgress.of(attempt).model_dump(mode="json") if attempt is not None else None
        ),
        "attempts": attempts,
    }


def _attempt_topic(attempt: mentor_api.ExamAttemptRead, topics: dict[uuid.UUID, str]) -> str:
    """Тема попытки: у назначенной — тема назначения, у своей — глава или весь кодекс."""
    if attempt.assignment_id in topics:
        return topics[attempt.assignment_id]
    return mentor_results.exam_topic(attempt)


def _is_question(message: mentor_api.MentorMessageRead) -> bool:
    return message.role is mentor_api.MessageRole.ASSISTANT and not message.cited_articles


def _topic_groups(codex: mentor_api.Codex) -> list[dict[str, Any]]:
    """Темы «Нового вопроса»: главы кодекса, сгруппированные по частям."""
    return [
        {
            "title": part.title,
            "chapters": [
                {"number": chapter.number, "title": f"Глава {chapter.number}. {chapter.title}"}
                for section in part.sections
                for chapter in section.chapters
            ],
        }
        for part in codex.parts
    ]


async def _codex_context(staff: StaffContext, zone: tzinfo) -> dict[str, Any]:
    """Дерево кодекса для страницы и, менеджеру, — блок загрузки со сведениями."""
    codex = await mentor_api.load_codex_document()
    context: dict[str, Any] = {"codex": codex, "can_upload": sees_names(staff), "uploaded": None}
    if sees_names(staff) and codex is not None:
        info = await mentor_api.get_codex()
        if info is not None:
            context["uploaded"] = (
                f"Загружен {info.uploaded_at.astimezone(zone):%d.%m.%Y %H:%M} · "
                f"статей — {info.article_count}"
            )
    return context
