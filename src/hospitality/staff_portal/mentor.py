"""Раздел «Наставник» кабинета (spec 0048 §2, §6–7; issue #492, PR 4 серии).

Четыре вкладки: «Спросить» и «Тренировка» — чат с потоком ответа
(`mentor_api.py`, `static/mentor_chat.js`), «Кодекс» — документ целиком с
поиском и фильтром «Для кого» (`static/mentor_codex.js`), «Где ошибаются» —
статистика «Тренировки». Данные — модуль `mentor`, ответы модели — `ai/mentor`;
здесь — права, контекст страниц и тексты сотруднику по кодам ошибок (§7).

Права (§6, docs/RBAC.md) — только `require_role` (P-12): вкладки чата и кодекса —
любой уровень, «Где ошибаются» — старший и менеджер, имена в ней, «Обнулить» и
загрузка кодекса — менеджер. Диалог сотрудника читает только он сам: каждый
вызов модуля — с `user_id` вошедшего. Раздел выключен у отеля
(`TenantConfig.mentor_enabled`, умолчание `false`) — пункта меню нет, страницы и
действия отвечают 404, как неизвестный адрес.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import tzinfo
from typing import Any, Final

from fastapi import HTTPException, Request

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import MAX_MESSAGE_CHARS
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform import staff_auth, staff_team
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import AppError
from hospitality.staff_portal import queue

ConversationMode = mentor_api.ConversationMode

# Вкладки в порядке полосы; «Где ошибаются» — только старшему и менеджеру.
TABS: Final = {"ask": "Спросить", "quiz": "Тренировка", "codex": "Кодекс", "stats": "Где ошибаются"}
_TEMPLATES: Final = {
    "ask": "mentor_chat.html",
    "quiz": "mentor_chat.html",
    "codex": "mentor_codex.html",
    "stats": "mentor_stats.html",
}
# Строки, где ответов меньше, — внизу серым: выводы по ним делать рано («Наставник»).
MIN_ANSWERS: Final = 5

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
    """Зависимость прав вкладки (§6): «Где ошибаются» — старшему и менеджеру."""
    return _senior_role if tab == "stats" else any_role


def sees_names(staff: StaffContext) -> bool:
    """Имена в «Где ошибаются», «Обнулить» и загрузка кодекса — только менеджеру (§6)."""
    return staff.role_key is StaffRole.MANAGER


def error_text(error: AppError) -> str:
    """Текст сотруднику по коду ошибки хода (§7); разметка кодекса — как есть."""
    if error.code == mentor_api.ERR_MENTOR_CODEX_MISSING:
        return NO_CODEX_TEXT
    if error.code == mentor_api.ERR_MENTOR_NO_QUIZ_QUESTION:
        return NO_QUESTION_TEXT
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
    context: dict[str, Any] = {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "tab": tab,
        "tabs": [
            {"key": key, "title": title, "href": path if key == "ask" else f"{path}/{key}"}
            for key, title in TABS.items()
            if key != "stats" or staff.role_key is not StaffRole.STAFF
        ],
        "codex_href": f"{path}/codex",
        "api": f"/staff/{staff.tenant_slug}/api/mentor",
        "no_codex_text": NO_CODEX_TEXT,
    }
    if tab in ("ask", "quiz"):
        context.update(await _chat_context(staff, ConversationMode(tab), path))
    elif tab == "codex":
        context.update(await _codex_context(staff, config.tzinfo))
    else:
        context.update(await _stats_context(staff, config.tzinfo))
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
    chapters: list[dict[str, Any]] = []
    if quiz and has_codex:
        codex = await mentor_api.load_codex_document()
        chapters = _topic_groups(codex) if codex else []
    return {
        "mode": mode.value,
        "has_codex": has_codex,
        "max_chars": MAX_MESSAGE_CHARS,
        "cut_text": CUT_TEXT,
        "unavailable_text": UNAVAILABLE_TEXT,
        "question_label": QUESTION_LABEL,
        "quiz_open": bool(conversation and conversation.quiz_question),
        "topic_groups": chapters,
        "messages": [
            {
                "role": "user" if m.role is mentor_api.MessageRole.USER else "bot",
                "text": m.text,
                "label": QUESTION_LABEL if quiz and _is_question(m) else None,
                "sources": [
                    {
                        "title": f"Статья {articles[n].number}. {articles[n].title}",
                        "href": f"{path}/codex#{articles[n].anchor}",
                    }
                    for n in m.cited_articles
                    if n in articles
                ],
            }
            for m in messages
        ],
    }


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
                f"Сейчас: загружен {info.uploaded_at.astimezone(zone):%d.%m.%Y %H:%M}, "
                f"статей — {info.article_count}."
            )
    return context


async def _stats_context(staff: StaffContext, zone: tzinfo) -> dict[str, Any]:
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
        "can_reset": sees_names(staff),
        "since": f"{since.astimezone(zone):%d.%m.%Y}" if since else None,
        "answers": sum(row.total for row in by_chapter),
        "min_answers": MIN_ANSWERS,
    }
    if sees_names(staff):
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
