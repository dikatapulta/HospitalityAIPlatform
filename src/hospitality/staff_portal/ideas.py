"""Раздел «Предложить идею» кабинета (spec 0071 §2–4, §8–9; #871, PR 3 серии).

Вкладки: «Новая идея» и «Интервью» — чат поверх «Наставника» (`templates/ideas_chat.html`,
`static/mentor_chat.js` + `static/ideas.js`; ход — `ai/mentor/ideas.py`), «Мои идеи» —
отправленные карточки автора, «Все идеи» — карточки всех авторов отеля
(`ideas_all.py`). Здесь — права, контекст страниц и тексты сотруднику (§9).

Права (§8, docs/RBAC.md): «Новая идея» и «Мои идеи» — любой уровень, только своё
(`user_id` вошедшего в каждом вызове модуля); «Интервью» — старший и менеджер;
«Все идеи» — только суперадмин (`staff_superadmin.require_superadmin`), менеджер отеля —
нет. Пометку ИИ автор не видит нигде (§6): в контекст его страниц она не попадает.
Раздел выключен у отеля (`TenantConfig.ideas_enabled`, умолчание `false`) — пункта меню
нет, страницы и действия отвечают 404, как неизвестный адрес.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import HTTPException, Request

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import MAX_MESSAGE_CHARS
from hospitality.ai.mentor.ideas import IDEA_DRAFT_LABEL, INTERVIEW_DRAFT_LABEL
from hospitality.ai.tools.propose_interview_summary import MAX_SUMMARY_CHARS
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform import staff_auth, staff_superadmin, staff_team
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import AppError
from hospitality.staff_portal import ideas_all, queue

ConversationMode = mentor_api.ConversationMode

# Тексты сотруднику (§9; показаны основателю 07.11 — docs/founder-review/).
NAV_TITLE: Final = "Предложить идею"
NAV_HINT: Final = "Расскажите, что улучшить — ИИ уточнит и передаст разработчику"
TABS: Final = {
    "new": "Новая идея",
    "mine": "Мои идеи",
    "interview": "Интервью",
    "all": ideas_all.TAB_TITLE,
}
IDEA_HELLO: Final = (
    "Здравствуйте, {name}! Расскажите, что в работе можно сделать удобнее с Necturn, — можно "
    "голосом, микрофоном на клавиатуре телефона. Я задам пару вопросов и передам идею "
    "разработчику. Её прочитает {reader} вместе с вашим именем — чтобы, если нужно, подойти и "
    "обсудить. Руководству отеля — только общие выводы, без имён."
)
INTERVIEW_HELLO: Final = (
    "Здравствуйте, {name}! Я помощник Necturn. Хочу понять, как устроена работа вашей службы и "
    "что в ней мешает, — чтобы система снимала с вас дела, а не добавляла. Это 15–20 минут, "
    "можно с перерывами. Ваши ответы с вашим именем прочитает {reader} — чтобы, если нужно, "
    "подойти и обсудить подробнее; руководству — только общие выводы, без имён. Начнём: как "
    "обычно начинается ваша смена?"
)
# `{читатель}` (§9): суперадмины отеля через «и»; нет ни одного — команда целиком.
READER_TEMPLATE: Final = "{names} из команды Necturn"
READER_TEAM: Final = "команда Necturn"
IDEA_PLACEHOLDER: Final = "Опишите идею или проблему своими словами"
INTERVIEW_PLACEHOLDER: Final = "Ваш ответ…"
SEND_TEXT: Final = "Отправить разработчику"
CONTINUE_TEXT: Final = "Продолжить разговор"
IDEA_SENT_TEXT: Final = "Спасибо! Идея записана и передана разработчику."
INTERVIEW_SENT_TEXT: Final = "Спасибо за разговор! Итог передан разработчику."
MINE_EMPTY: Final = "Здесь появятся идеи, которые вы отправили."
DELETE_CONFIRM: Final = "Удалить идею? Разработчик её больше не увидит."
BUDGET_TEXT: Final = "На сегодня я выслушал всё, что мог. Напишите завтра — разговор сохранится."
UNAVAILABLE_TEXT: Final = (
    "Помощник сейчас не отвечает. Попробуйте через минуту — разговор сохранится."
)
ALREADY_SENT_TEXT: Final = "Эта идея уже отправлена — обновите страницу."
KIND_TITLES: Final = {mentor_api.IdeaKind.IDEA: "Идея", mentor_api.IdeaKind.INTERVIEW: "Интервью"}

# Предел пересказа в поле черновика — больший из двух инструментов (итог интервью).
MAX_DRAFT_CHARS: Final = MAX_SUMMARY_CHARS

_CHAT_MODES: Final = {"new": ConversationMode.IDEA, "interview": ConversationMode.INTERVIEW}
_TEMPLATES: Final = {
    "new": "ideas_chat.html",
    "interview": "ideas_chat.html",
    "mine": "ideas_mine.html",
    "all": "ideas_all.html",
}

any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
senior_role: Final = staff_auth.require_role(StaffRole.SENIOR, StaffRole.MANAGER)


def section_on(config: TenantConfig | None) -> bool:
    """Раздел включён у отеля (флаг тенанта, §2); отель без конфига — нет."""
    return config is not None and config.ideas_enabled


async def require_section(staff: StaffContext) -> TenantConfig:
    """Конфиг отеля с включённым разделом; выключен — 404, как неизвестный адрес."""
    config = await queue.tenant_config(staff)
    if config is None or not section_on(config):
        raise HTTPException(status_code=404)
    return config


def page_role(tab: str) -> Callable[[Request], Awaitable[StaffContext]]:
    """Зависимость прав вкладки (§8): «Интервью» — старшему и менеджеру, «Все идеи» —
    суперадмину; неизвестная вкладка — любому уровню, а `build_page` ответит 404."""
    if tab == "all":
        return staff_superadmin.require_superadmin
    return senior_role if tab == "interview" else any_role


def mode_role(mode: ConversationMode) -> Callable[[Request], Awaitable[StaffContext]]:
    """Права хода и «Начать заново» режима: интервью — старшему и менеджеру."""
    return senior_role if mode is ConversationMode.INTERVIEW else any_role


def _tab_visible(key: str, staff: StaffContext) -> bool:
    if key == "all":
        return staff.is_platform_admin and staff.role_key is StaffRole.MANAGER
    return key != "interview" or staff.role_key is not StaffRole.STAFF


def error_text(error: AppError) -> str:
    """Текст автору по коду ошибки хода (§9)."""
    if error.code == gateway.ERR_AI_BUDGET_EXCEEDED:
        return BUDGET_TEXT
    if error.code == mentor_api.ERR_MENTOR_NO_IDEA_DRAFT:
        return ALREADY_SENT_TEXT
    return UNAVAILABLE_TEXT


def base_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/ideas"


def reader_names(names: list[str]) -> str:
    """`{читатель}`: «Али», «Али и Аян», «Али, Аян и Ая»; никого — «команда Necturn»."""
    names = [name.strip() for name in names if name.strip()]
    if not names:
        return READER_TEAM
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} и {names[-1]}"
    return READER_TEMPLATE.format(names=joined)


async def _reader(staff: StaffContext) -> str:
    """Суперадмины этого отеля с действующим доступом — те, кто откроет «Все идеи»."""
    members = await staff_team.list_tenant_members(staff.tenant_id)
    return reader_names(
        [
            member.display_name
            for member in members
            if member.is_platform_admin
            and member.membership_status is MembershipStatus.ACTIVE
            and member.user_status is UserStatus.ACTIVE
        ]
    )


async def build_page(
    staff: StaffContext, tab: str, *, show_all: bool = False
) -> tuple[str, dict[str, Any]]:
    """Шаблон и контекст вкладки. Раздел выключен или вкладки нет — 404.

    `show_all` — фильтр «Все» вкладки «Все идеи» (без него — «Не разобранные»).
    """
    if tab not in TABS:
        raise HTTPException(status_code=404)
    config = await require_section(staff)
    path = base_path(staff.tenant_slug)
    context: dict[str, Any] = {
        "tenant_name": staff.tenant_name,
        "tab": tab,
        "tabs": [
            {"key": key, "title": title, "href": path if key == "new" else f"{path}/{key}"}
            for key, title in TABS.items()
            if _tab_visible(key, staff)
        ],
        "hint": NAV_HINT,
        "api": f"/staff/{staff.tenant_slug}/api/ideas",
    }
    if tab in _CHAT_MODES:
        context.update(await _chat_context(staff, _CHAT_MODES[tab]))
    elif tab == "mine":
        context.update(await _mine_context(staff, config))
    else:
        context.update(await ideas_all.all_context(staff, config.tzinfo, show_all=show_all))
    return _TEMPLATES[tab], context


async def _chat_context(staff: StaffContext, mode: ConversationMode) -> dict[str, Any]:
    """Лента открытого диалога режима и черновик — без пометки ИИ (§6).

    Карточка черновика стоит под лентой, только если последний ход — предложение:
    ответ хода тогда кончается подписью черновика (`ai/mentor/ideas.py`). Автор
    продолжил разговор — карточки нет до следующего предложения.
    """
    interview = mode is ConversationMode.INTERVIEW
    label = INTERVIEW_DRAFT_LABEL if interview else IDEA_DRAFT_LABEL
    conversation = await mentor_api.get_conversation(staff.user_id, mode)
    messages = conversation.messages if conversation else []
    draft = await mentor_api.get_idea_draft(staff.user_id, mode)
    last = messages[-1] if messages else None
    proposed = (
        last is not None
        and last.role is mentor_api.MessageRole.ASSISTANT
        and last.text.endswith(label)
    )
    hello = INTERVIEW_HELLO if interview else IDEA_HELLO
    return {
        "mode": mode.value,
        "hello": hello.format(name=staff.display_name.strip(), reader=await _reader(staff)),
        "placeholder": INTERVIEW_PLACEHOLDER if interview else IDEA_PLACEHOLDER,
        "max_chars": MAX_MESSAGE_CHARS,
        "max_draft_chars": MAX_DRAFT_CHARS,
        # Связь оборвалась посреди ответа — тот же текст: ход мог и записаться.
        "cut_text": UNAVAILABLE_TEXT,
        "unavailable_text": UNAVAILABLE_TEXT,
        "texts": {
            "send": SEND_TEXT,
            "continue": CONTINUE_TEXT,
            "sent": INTERVIEW_SENT_TEXT if interview else IDEA_SENT_TEXT,
            "already_sent": ALREADY_SENT_TEXT,
            "unavailable": UNAVAILABLE_TEXT,
        },
        "messages": [
            {
                "role": "user" if m.role is mentor_api.MessageRole.USER else "bot",
                "text": m.text,
            }
            for m in messages
        ],
        "draft": (
            {"id": str(draft.id), "summary": draft.summary}
            if draft is not None and proposed
            else None
        ),
    }


async def _mine_context(staff: StaffContext, config: TenantConfig) -> dict[str, Any]:
    """«Мои идеи»: отправленные карточки автора без пометки ИИ (§6), свежие сверху."""
    zone = config.tzinfo
    return {
        "empty_text": MINE_EMPTY,
        "delete_confirm": DELETE_CONFIRM,
        "ideas": [
            {
                "id": str(idea.id),
                "kind": KIND_TITLES[idea.kind],
                "when": f"{idea.sent_at.astimezone(zone):%d.%m.%Y %H:%M}",
                "summary": idea.summary,
            }
            for idea in await mentor_api.list_my_ideas(staff.user_id)
            if idea.sent_at is not None
        ],
    }
