"""«Все идеи» — карточки всех авторов отеля суперадмину и выгрузка в Markdown (spec 0071 §8).

Права — только суперадмин (`ideas.page_role`, `staff_superadmin.require_superadmin`):
руководство отеля карточек и разговоров не видит (решение сессии, §8). Автор — имя и
нынешние службы из «Сотрудников»; стёртой учётки там нет (spec 0044 §5) — карточка
остаётся, вместо имени пометка. Каждый просмотр и выгрузка — событие лога
`superadmin.ideas_viewed` / `superadmin.ideas_exported` с `user_id` суперадмина, без
текстов (§8). Выгрузка — один файл для окна LLM по подписке; в репозиторий не кладётся.
"""

from __future__ import annotations

import uuid
from datetime import datetime, tzinfo
from typing import Any, Final

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.staff_portal.team import service_choices

logger = get_logger(module=__name__)

# Тексты основателю (§8; список — docs/founder-review/2026-11-07-pr883-ideas-cabinet.md).
TAB_TITLE: Final = "Все идеи"
FILTER_NEW: Final = "Не разобранные"
FILTER_ALL: Final = "Все"
REVIEWED_TEXT: Final = "Разобрано"
CONVERSATION_TEXT: Final = "Разговор"
CONVERSATION_GONE: Final = "Разговор удалён по сроку хранения"
EXPORT_TEXT: Final = "Скачать всё (Markdown)"
EMPTY_TEXT: Final = "Идей пока нет."
UNKNOWN_AUTHOR: Final = "Учётки больше нет в «Сотрудниках»"
MARK_TITLES: Final = {
    mentor_api.IdeaMark.EXISTS: "Уже есть",
    mentor_api.IdeaMark.SIMILAR: "Есть похожее",
    mentor_api.IdeaMark.FEASIBLE: "Можно сделать",
    mentor_api.IdeaMark.CONSTRAINT: "Упирается в ограничение",
    mentor_api.IdeaMark.UNKNOWN: "Не знаю — решит разработчик",
}
_KIND_TITLES: Final = {
    mentor_api.IdeaKind.IDEA: "Идея",
    mentor_api.IdeaKind.INTERVIEW: "Интервью",
}
_ROLE_TITLES: Final = {
    mentor_api.MessageRole.USER: "Сотрудник",
    mentor_api.MessageRole.ASSISTANT: "ИИ",
}


async def _authors(staff: StaffContext) -> dict[uuid.UUID, tuple[str, str]]:
    """`user_id` → (имя, службы через запятую) по нынешним «Сотрудникам»."""
    names = {
        choice["key"]: choice["name"]
        for choice in service_choices(await requests_api.list_categories())
    }
    return {
        member.user_id: (
            member.display_name,
            ", ".join(names.get(key, key) for key in member.service_keys),
        )
        for member in await staff_team.list_tenant_members(staff.tenant_id)
    }


def _when(moment: datetime | None, zone: tzinfo) -> str:
    return "" if moment is None else f"{moment.astimezone(zone):%d.%m.%Y %H:%M}"


def _card(
    entry: mentor_api.SentIdeaRead,
    authors: dict[uuid.UUID, tuple[str, str]],
    zone: tzinfo,
) -> dict[str, Any]:
    idea = entry.idea
    name, services = authors.get(idea.user_id, (UNKNOWN_AUTHOR, ""))
    return {
        "id": str(idea.id),
        "author": name,
        "services": services,
        "kind": _KIND_TITLES[idea.kind],
        "when": _when(idea.sent_at, zone),
        "mark": MARK_TITLES[idea.mark] if idea.mark is not None else None,
        "mark_note": idea.mark_note,
        "summary": idea.summary,
        "reviewed": _when(idea.reviewed_at, zone) or None,
        "messages": [
            {"role": "user" if m.role is mentor_api.MessageRole.USER else "bot", "text": m.text}
            for m in entry.messages
        ],
    }


async def all_context(
    staff: StaffContext, zone: tzinfo, *, show_all: bool = False
) -> dict[str, Any]:
    """Карточки «Все идеи»: по умолчанию — не разобранные, свежие сверху."""
    entries = await mentor_api.list_sent_ideas(unreviewed_only=not show_all)
    authors = await _authors(staff)
    logger.info("superadmin.ideas_viewed", user_id=str(staff.user_id), cards=len(entries))
    return {
        "show_all": show_all,
        "texts": {
            "filter_new": FILTER_NEW,
            "filter_all": FILTER_ALL,
            "reviewed": REVIEWED_TEXT,
            "conversation": CONVERSATION_TEXT,
            "conversation_gone": CONVERSATION_GONE,
            "export": EXPORT_TEXT,
            "empty": EMPTY_TEXT,
        },
        "cards": [_card(entry, authors, zone) for entry in entries],
    }


async def export_markdown(staff: StaffContext, zone: tzinfo) -> str:
    """Все отправленные карточки отеля с разговорами — один Markdown-файл (§8)."""
    entries = await mentor_api.list_sent_ideas()
    authors = await _authors(staff)
    lines = [
        f"# Идеи и интервью сотрудников — {staff.tenant_name}",
        "",
        f"Выгрузка {_when(utc_now(), zone)}, карточек: {len(entries)}.",
    ]
    for entry in entries:
        card = _card(entry, authors, zone)
        author = f"{card['author']} ({card['services']})" if card["services"] else card["author"]
        lines += ["", f"## {card['kind']} — {author} — {card['when']}", ""]
        if card["mark"]:
            note = f" — {card['mark_note']}" if card["mark_note"] else ""
            lines += [f"Пометка ИИ: {card['mark']}{note}", ""]
        if card["reviewed"]:
            lines += [f"{REVIEWED_TEXT}: {card['reviewed']}", ""]
        lines += [card["summary"], "", f"### {CONVERSATION_TEXT}", ""]
        if not entry.messages:
            lines.append(CONVERSATION_GONE)
        for message in entry.messages:
            lines += [f"**{_ROLE_TITLES[message.role]}:** {message.text}", ""]
    logger.info("superadmin.ideas_exported", user_id=str(staff.user_id), cards=len(entries))
    return "\n".join(lines).rstrip() + "\n"
