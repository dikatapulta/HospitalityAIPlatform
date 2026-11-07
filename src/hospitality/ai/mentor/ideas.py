"""«Предложить идею» и интервью служб: ход разговора (spec 0071 §3–6).

Ход: сообщение автора → один `complete(purpose="ideas")` с инструментом
черновика (не принудительным; на пределе сообщений — принудительным) → текстом
модель задаёт вопрос, вызовом инструмента предлагает черновик карточки → запись
реплик и черновика → события `delta` и `done` (тот же протокол, что у
«Наставника», одним куском: поток не нужен, §3 п. 6).

Первое сообщение раздела — текст кабинета с именами автора и читателя; модели
он не уходит (§10: имя и логин в Anthropic не передаются) — промпт лишь говорит,
что приветствие было и о чём спросило. Текущий черновик модель видит в системном
промпте: правку автора до отправки она не видит, модель её и не зовёт (§3 п. 3).
Ошибки шлюза (ERR-AI-001/002/003/006) уходят вызывающему — текст автору по коду
выбирает кабинет (§9).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import (
    MentorDelta,
    MentorDone,
    MentorEvent,
    MentorProviders,
    require_text,
)
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import propose_idea_card, propose_interview_summary
from hospitality.modules.mentor import api as mentor
from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

IDEA_PROMPT: Final = "ideas_v1"
INTERVIEW_PROMPT: Final = "interview_v1"
CAPABILITY_MAP: Final = "ideas_capability_map"
IDEA_MAX_TOKENS: Final = 600
# Итог интервью длиннее вопроса; резерв шлюза — лишь верхняя граница (§4).
INTERVIEW_MAX_TOKENS: Final = 1500
# На этом сообщении автора и дальше черновик принудителен (§3 п. 5, §4).
IDEA_MESSAGE_LIMIT: Final = 12
INTERVIEW_MESSAGE_LIMIT: Final = 40

# Подпись черновика (§9): последней строкой реплики, следом — карточка в кабинете.
IDEA_DRAFT_LABEL: Final = "Так я понял вашу идею — поправьте, если нужно"
INTERVIEW_DRAFT_LABEL: Final = "Итог разговора — поправьте, если что-то не так"


@dataclass(frozen=True)
class Proposal:
    """Черновик из вызова инструмента: у итога интервью пометки нет."""

    summary: str
    mark: mentor.IdeaMark | None = None
    mark_note: str | None = None


def _idea_proposal(arguments: dict[str, Any]) -> Proposal | None:
    card = propose_idea_card.parse(arguments)
    return None if card is None else Proposal(card.summary, card.mark, card.mark_note)


def _interview_proposal(arguments: dict[str, Any]) -> Proposal | None:
    result = propose_interview_summary.parse(arguments)
    return None if result is None else Proposal(result.summary)


@dataclass(frozen=True)
class _Flow:
    prompt: str
    tool_name: str
    build_tool: Callable[[], gateway.ToolSpec]
    parse: Callable[[dict[str, Any]], Proposal | None]
    max_tokens: int
    message_limit: int
    label: str
    # Карта возможностей — только идее: интервью слушает, а не оценивает (§4).
    with_map: bool


_FLOWS: Final = {
    mentor.ConversationMode.IDEA: _Flow(
        prompt=IDEA_PROMPT,
        tool_name=propose_idea_card.NAME,
        build_tool=propose_idea_card.build_spec,
        parse=_idea_proposal,
        max_tokens=IDEA_MAX_TOKENS,
        message_limit=IDEA_MESSAGE_LIMIT,
        label=IDEA_DRAFT_LABEL,
        with_map=True,
    ),
    mentor.ConversationMode.INTERVIEW: _Flow(
        prompt=INTERVIEW_PROMPT,
        tool_name=propose_interview_summary.NAME,
        build_tool=propose_interview_summary.build_spec,
        parse=_interview_proposal,
        max_tokens=INTERVIEW_MAX_TOKENS,
        message_limit=INTERVIEW_MESSAGE_LIMIT,
        label=INTERVIEW_DRAFT_LABEL,
        with_map=False,
    ),
}

IDEA_MODES: Final = frozenset(_FLOWS)


class IdeaDraftView(BaseModel):
    """Черновик для автора — без пометки ИИ: её видит только основатель (§6)."""

    id: uuid.UUID
    summary: str


class IdeaDone(MentorDone):
    """`done` хода идеи: ответ и, если модель предложила, черновик карточки."""

    draft: IdeaDraftView | None = None


def build_request(
    mode: mentor.ConversationMode,
    history: Sequence[mentor.MentorMessageRead],
    message: str,
    *,
    draft: str | None,
) -> gateway.LlmRequest:
    """Запрос хода — один на кабинет и на evals (§11 п. 3)."""
    flow = _FLOWS[mode]
    authored = sum(1 for m in history if m.role == mentor.MessageRole.USER) + 1
    system = load_prompt(flow.prompt).strip()
    if flow.with_map:
        system += f"\n\n{load_prompt(CAPABILITY_MAP).strip()}"
    if draft:
        system += (
            f"\n\nThe current draft the employee sees (they may edit it before sending):\n{draft}"
        )
    return gateway.LlmRequest(
        system=system,
        messages=[
            *(gateway.LlmMessage(role=m.role.value, content=m.text) for m in history),
            gateway.LlmMessage(role="user", content=message),
        ],
        max_tokens=flow.max_tokens,
        tools=[flow.build_tool()],
        forced_tool=flow.tool_name if authored >= flow.message_limit else None,
        purpose="ideas",
    )


async def idea_turn(
    user_id: uuid.UUID,
    mode: mentor.ConversationMode,
    message: str,
    *,
    providers: MentorProviders | None = None,
) -> AsyncGenerator[MentorEvent, None]:
    """Ход автора: `MentorDelta` с ответом, затем `IdeaDone`. Внутри `tenant_context` (P-4).

    Записи — до событий: модель ответила целиком, ответ и черновик сохранены,
    даже если автор закрыл страницу. Пустой ответ без черновика — ERR-AI-003.
    """
    flow = _FLOWS[mode]
    providers = providers or MentorProviders()
    text = message.strip()
    if not text:
        raise ValueError("idea_turn: пустое сообщение")
    asked_at = utc_now()
    conversation = await mentor.get_conversation(user_id, mode)
    history = conversation.history if conversation else []
    draft = await mentor.get_idea_draft(user_id, mode)
    request = build_request(mode, history, text, draft=draft.summary if draft else None)
    response = await gateway.complete(request, provider=providers.chat)
    call = next((c for c in response.tool_calls if c.name == flow.tool_name), None)
    proposal = flow.parse(call.arguments) if call is not None else None
    if call is not None and proposal is None:
        logger.warning("mentor_idea_bad_proposal", mode=mode.value)
    lead = response.text.strip()
    if proposal is None:
        answer = require_text(response, flow=f"{mode.value}_turn")
    else:
        answer = f"{lead}\n\n{flow.label}" if lead else flow.label
    conversation = await mentor.append_messages(
        user_id,
        mode,
        [
            mentor.NewMentorMessage(role=mentor.MessageRole.USER, text=text, created_at=asked_at),
            mentor.NewMentorMessage(
                role=mentor.MessageRole.ASSISTANT, text=answer, created_at=utc_now()
            ),
        ],
    )
    view: IdeaDraftView | None = None
    if proposal is not None:
        saved = await mentor.save_idea_draft(
            user_id,
            conversation.id,
            summary=proposal.summary,
            mark=proposal.mark,
            mark_note=proposal.mark_note,
        )
        view = None if saved is None else IdeaDraftView(id=saved.id, summary=saved.summary)
    logger.info(
        "mentor_idea_turn",
        mode=mode.value,
        proposed=view is not None,
        forced=request.forced_tool is not None,
        author_messages=sum(1 for m in conversation.messages if m.role == mentor.MessageRole.USER),
    )
    yield MentorDelta(text=answer)
    yield IdeaDone(answer=answer, sources=[], draft=view)
