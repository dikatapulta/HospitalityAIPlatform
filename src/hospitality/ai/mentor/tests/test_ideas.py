"""«Предложить идею» и интервью: ход разговора (spec 0071 §3–6, §12 PR 3) — на Fake-адаптере.

Текст модели — уточняющий вопрос, вызов инструмента — черновик карточки с пометкой
(автору её не отдают); на 12-м сообщении идеи и 40-м интервью инструмент
принудителен; имя автора к модели не уходит; карта возможностей — только в идее.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest

from hospitality.ai.evals.staff_ideas import CASES, text_problems
from hospitality.ai.gateway.api import (
    ERR_AI_PROVIDER_ERROR,
    LlmRequest,
    MockTurn,
    ScriptedLlmProvider,
    ToolCall,
)
from hospitality.ai.mentor import ideas
from hospitality.ai.mentor.common import MentorDelta, MentorEvent, MentorProviders
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools import propose_idea_card, propose_interview_summary
from hospitality.modules.mentor import api as mentor
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

IDEA = mentor.ConversationMode.IDEA
INTERVIEW = mentor.ConversationMode.INTERVIEW
QUESTION = "Понял. А как сейчас горничная узнаёт, что номер свободен?"
SUMMARY = "Горничной нужен сигнал о выезде сразу, без звонка ресепшена."
MAP_MARK = "Карта возможностей Necturn"


def _card(summary: str = SUMMARY) -> ToolCall:
    return ToolCall(
        id="call-1",
        name=propose_idea_card.NAME,
        arguments={"summary": summary, "mark": "exists", "mark_note": "Выезды и уборка"},
    )


def _scripted(*turns: MockTurn) -> tuple[MentorProviders, ScriptedLlmProvider]:
    chat = ScriptedLlmProvider(list(turns), model="claude-sonnet-5")
    return MentorProviders(chat=chat), chat


async def _turn(
    user_id: uuid.UUID,
    mode: mentor.ConversationMode,
    text: str,
    fakes: MentorProviders,
) -> list[MentorEvent]:
    events: AsyncGenerator[MentorEvent, None] = ideas.idea_turn(
        user_id, mode, text, providers=fakes
    )
    return [event async for event in events]


def _done(events: list[MentorEvent]) -> ideas.IdeaDone:
    delta, done = events
    assert isinstance(delta, MentorDelta)
    assert isinstance(done, ideas.IdeaDone)
    assert delta.text == done.answer
    return done


@pytest.mark.usefixtures("ideas_on")
async def test_text_is_a_question_and_tool_call_is_a_draft(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes, chat = _scripted(
        MockTurn(text=QUESTION),
        MockTurn(text="Похоже, это уже есть: «Выезды и уборка».", tool_calls=[_card()]),
    )
    with tenant_context(tenant_id):
        asked = _done(await _turn(user_id, IDEA, "Хочу, чтобы уборка шла быстрее", fakes))
        assert (asked.answer, asked.draft) == (QUESTION, None)
        assert await mentor.get_idea_draft(user_id, IDEA) is None

        proposed = _done(await _turn(user_id, IDEA, "Ресепшен звонит горничной", fakes))
        assert proposed.answer == (
            f"Похоже, это уже есть: «Выезды и уборка».\n\n{ideas.IDEA_DRAFT_LABEL}"
        )
        assert proposed.draft is not None and proposed.draft.summary == SUMMARY
        # Автору — только id и пересказ: пометки в событии нет (§6).
        assert set(proposed.model_dump()["draft"]) == {"id", "summary"}
        stored = await mentor.get_idea_draft(user_id, IDEA)
        assert stored is not None and stored.id == proposed.draft.id
        assert (stored.mark, stored.mark_note) == (mentor.IdeaMark.EXISTS, "Выезды и уборка")

        conversation = await mentor.get_conversation(user_id, IDEA)
        assert conversation is not None
        assert [m.text for m in conversation.messages] == [
            "Хочу, чтобы уборка шла быстрее",
            QUESTION,
            "Ресепшен звонит горничной",
            proposed.answer,
        ]

    first, second = chat.calls
    assert (first.purpose, first.max_tokens, first.forced_tool) == ("ideas", 600, None)
    assert [tool.name for tool in first.tools] == [propose_idea_card.NAME]
    assert first.system is not None and MAP_MARK in first.system
    assert [(m.role, m.content) for m in second.messages][:2] == [
        ("user", "Хочу, чтобы уборка шла быстрее"),
        ("assistant", QUESTION),
    ]


@pytest.mark.usefixtures("ideas_on")
async def test_next_turn_sees_the_current_draft_and_replaces_it(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes, chat = _scripted(
        MockTurn(tool_calls=[_card()]),
        MockTurn(tool_calls=[_card("Новый пересказ")]),
    )
    with tenant_context(tenant_id):
        first = _done(await _turn(user_id, IDEA, "Отправьте как есть", fakes))
        assert first.answer == ideas.IDEA_DRAFT_LABEL
        second = _done(await _turn(user_id, IDEA, "Добавьте: и ночью тоже", fakes))
        assert second.draft is not None and first.draft is not None
        assert second.draft.id == first.draft.id
        stored = await mentor.get_idea_draft(user_id, IDEA)
        assert stored is not None and stored.summary == "Новый пересказ"

    assert chat.calls[0].system is not None and SUMMARY not in chat.calls[0].system
    assert chat.calls[1].system is not None and SUMMARY in chat.calls[1].system


async def _seed_history(user_id: uuid.UUID, mode: mentor.ConversationMode, authored: int) -> None:
    now = utc_now()
    messages = []
    for index in range(authored):
        messages += [
            mentor.NewMentorMessage(
                role=mentor.MessageRole.USER, text=f"ответ {index}", created_at=now
            ),
            mentor.NewMentorMessage(
                role=mentor.MessageRole.ASSISTANT, text=f"вопрос {index}", created_at=now
            ),
        ]
    await mentor.append_messages(user_id, mode, messages)


@pytest.mark.usefixtures("ideas_on")
@pytest.mark.parametrize(
    ("mode", "limit", "tool"),
    [
        (IDEA, ideas.IDEA_MESSAGE_LIMIT, propose_idea_card.NAME),
        (INTERVIEW, ideas.INTERVIEW_MESSAGE_LIMIT, propose_interview_summary.NAME),
    ],
)
async def test_tool_is_forced_on_the_limit_message(
    hotel: tuple[uuid.UUID, uuid.UUID], mode: mentor.ConversationMode, limit: int, tool: str
) -> None:
    """§3 п. 5 и §4: на 12-м сообщении идеи и 40-м интервью черновик принудителен."""
    tenant_id, user_id = hotel
    fakes, chat = _scripted(MockTurn(text="ещё вопрос"), MockTurn(text="и ещё"))
    with tenant_context(tenant_id):
        await _seed_history(user_id, mode, limit - 2)
        await _turn(user_id, mode, "предпоследнее", fakes)
        await _turn(user_id, mode, "последнее", fakes)
    before, on_limit = chat.calls
    assert before.forced_tool is None
    assert on_limit.forced_tool == tool


@pytest.mark.usefixtures("ideas_on")
async def test_interview_has_no_map_and_no_mark(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    summary_call = ToolCall(
        id="call-1",
        name=propose_interview_summary.NAME,
        arguments={"summary": "Смена: сверка заездов.", "mark": "exists"},
    )
    fakes, chat = _scripted(MockTurn(text="Спасибо!", tool_calls=[summary_call]))
    with tenant_context(tenant_id):
        done = _done(await _turn(user_id, INTERVIEW, "Хватит, давайте итог", fakes))
        assert done.answer == f"Спасибо!\n\n{ideas.INTERVIEW_DRAFT_LABEL}"
        stored = await mentor.get_idea_draft(user_id, INTERVIEW)
        assert stored is not None
        assert (stored.kind, stored.mark, stored.mark_note) == (
            mentor.IdeaKind.INTERVIEW,
            None,
            None,
        )
    [request] = chat.calls
    assert request.max_tokens == ideas.INTERVIEW_MAX_TOKENS
    assert request.system is not None and MAP_MARK not in request.system
    assert request.system.startswith(load_prompt(ideas.INTERVIEW_PROMPT).strip())


@pytest.mark.usefixtures("ideas_on")
async def test_author_name_never_reaches_the_model(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    """§10 и запись «потом» ревью #878: приветствие с именами — текст кабинета, не модели."""
    tenant_id, user_id = hotel
    fakes, chat = _scripted(MockTurn(text=QUESTION), MockTurn(text=QUESTION))
    with tenant_context(tenant_id):
        await _turn(user_id, IDEA, "Идея про уборку", fakes)
        await _turn(user_id, INTERVIEW, "Смена начинается в восемь", fakes)
    for request in chat.calls:
        assert "Анна" not in _request_text(request)


@pytest.mark.usefixtures("ideas_on")
async def test_empty_answer_without_card_is_an_error_and_nothing_is_saved(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    broken = ToolCall(id="call-1", name=propose_idea_card.NAME, arguments={"summary": "  "})
    fakes, _ = _scripted(MockTurn(text="", tool_calls=[broken]))
    with tenant_context(tenant_id):
        with pytest.raises(AppError) as error:
            await _turn(user_id, IDEA, "Идея", fakes)
        assert error.value.code == ERR_AI_PROVIDER_ERROR
        assert await mentor.get_conversation(user_id, IDEA) is None


def test_capability_map_names_every_non_goal_it_must() -> None:
    text = load_prompt(ideas.CAPABILITY_MAP)
    for number in range(2, 8):
        assert f"NG-{number}" in text


def _request_text(request: LlmRequest) -> str:
    return "\n".join([request.system or "", *(m.content for m in request.messages)])


def test_eval_checker_tells_soft_no_from_final_no_and_promises() -> None:
    """Проверка evals (spec 0071 §11 п. 3) сама проверена: образец §6 проходит."""
    sample = (
        "Понял идею. На первый взгляд хранить карту гостя у нас не получится — это запрещено "
        "правилами для платёжных данных, — но разработчик посмотрит и скажет точнее."
    )
    assert text_problems("ru", sample) == []
    assert text_problems("ru", "Так сделать невозможно.") == ["окончательный отказ"]
    assert text_problems("ru", "Сделаем через 2 недели!") == ["обещание или срок"]
    assert text_problems("ru", "Сколько раз за неделю это бывает?") == []
    assert text_problems("en", "Понял") == ["не по-английски"]
    assert text_problems("kk", "Түсіндім, рақмет!") == []
    assert len(CASES) == 12
    assert sum(len(case.turns) for case in CASES) == 33
