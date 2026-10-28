"""«Спросить» (spec 0048 §5.1–5.4, §11): поиск, поток, ссылки, запись, резюме — на Mock."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    ERR_AI_PURPOSE_NOT_CONFIGURED,
    MockLlmProvider,
)
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.mentor.ask import ask
from hospitality.ai.mentor.codex_upload import upload_codex
from hospitality.ai.mentor.common import MentorDelta, MentorDone, MentorEvent, MentorProviders
from hospitality.ai.mentor.tests.conftest import codex_files, providers
from hospitality.modules.mentor import api as mentor
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

ANSWER = "Поприветствуйте гостя [Статья 2]. Проверьте документ [Статья 3]. См. [Статья 99]."


async def _collect(events: AsyncGenerator[MentorEvent, None]) -> list[MentorEvent]:
    return [event async for event in events]


async def _article_text(number: str) -> str:
    return (await mentor.get_articles([number]))[number].full_text


@pytest.mark.usefixtures("mentor_enabled")
async def test_answer_streams_cites_known_articles_and_is_saved(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes = providers(answer=ANSWER)
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        # Вектор Mock — sha256 текста: вопрос, равный тексту статьи 2, находит её первой.
        question = await _article_text("2")
        events = await _collect(ask(user_id, question, providers=fakes))

        *deltas, done = events
        assert all(isinstance(event, MentorDelta) for event in deltas)
        assert "".join(event.text for event in deltas if isinstance(event, MentorDelta)) == ANSWER
        assert isinstance(done, MentorDone)
        assert done.answer == ANSWER
        # Ссылка на несуществующую статью 99 отброшена.
        assert [(s.number, s.title, s.anchor) for s in done.sources] == [
            ("2", "Встреча гостя у стойки", "st-2"),
            ("3", "Проверка брони", "st-3"),
        ]

        assert isinstance(fakes.streaming, MockLlmProvider)
        [request] = fakes.streaming.calls
        assert request.purpose == "mentor"
        assert request.max_tokens == 1024
        assert request.system is not None
        articles_block = request.system.split("Статьи кодекса:\n\n", 1)[1]
        assert articles_block.startswith("[Статья 2] Глава 2. Администратор\nСтатья 2. Встреча")
        assert [(m.role, m.content) for m in request.messages] == [("user", question)]

        conversation = await mentor.get_conversation(user_id, mentor.ConversationMode.ASK)
        assert conversation is not None
        assert [(m.role, m.text, m.cited_articles) for m in conversation.messages] == [
            (mentor.MessageRole.USER, question, []),
            (mentor.MessageRole.ASSISTANT, ANSWER, ["2", "3"]),
        ]
        async with session_scope() as session:
            rows = list(await session.scalars(select(LlmCallLog)))
        # Эмбеддинги кодекса, эмбеддинг вопроса, поток ответа — всё из бюджета наставника.
        assert {row.purpose for row in rows} == {"mentor"}
        assert len(rows) == 3


@pytest.mark.usefixtures("mentor_enabled")
async def test_short_follow_up_is_searched_with_previous_question(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        await _collect(ask(user_id, "Как встречать гостя у стойки?", providers=fakes))
        await _collect(ask(user_id, "А если я на звонке?", providers=fakes))

    assert fakes.embeddings is not None
    assert fakes.embeddings.calls[-1] == ["Как встречать гостя у стойки?\nА если я на звонке?"]  # type: ignore[attr-defined]
    assert isinstance(fakes.streaming, MockLlmProvider)
    second = fakes.streaming.calls[-1]
    assert [m.role for m in second.messages] == ["user", "assistant", "user"]


@pytest.mark.usefixtures("mentor_enabled")
async def test_aborted_answer_is_not_remembered(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    fakes = providers(answer="Длинный ответ из многих слов")
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        events = ask(user_id, "Как встречать гостя?", providers=fakes)
        first = await anext(events)
        assert isinstance(first, MentorDelta)
        await events.aclose()  # сотрудник ушёл посреди ответа

        assert await mentor.get_conversation(user_id, mentor.ConversationMode.ASK) is None
        async with session_scope() as session:
            statuses = list(await session.scalars(select(LlmCallLog.status)))
        assert LlmCallStatus.ABORTED in statuses


@pytest.mark.usefixtures("mentor_enabled")
async def test_no_codex_is_an_error_before_any_model_call(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id), pytest.raises(AppError) as error:
        await _collect(ask(user_id, "Вопрос", providers=fakes))
    assert error.value.code == mentor.ERR_MENTOR_CODEX_MISSING
    assert fakes.embeddings is not None and fakes.embeddings.calls == []  # type: ignore[attr-defined]


async def test_disabled_purpose_refuses_before_provider(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Пустой `LLM_MODEL_MENTOR` (dev, CI, staging) — ERR-AI-006, кодекс засеян без векторов."""
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id):
        await mentor.replace_codex(
            codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
        with pytest.raises(AppError) as error:
            await _collect(ask(user_id, "Вопрос", providers=fakes))
    assert error.value.code == ERR_AI_PURPOSE_NOT_CONFIGURED
    assert fakes.embeddings is not None and fakes.embeddings.calls == []  # type: ignore[attr-defined]


@pytest.mark.usefixtures("mentor_enabled")
async def test_seeded_codex_gets_vectors_on_first_question(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id):
        await mentor.replace_codex(
            codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
        await _collect(ask(user_id, "Как встречать гостя?", providers=fakes))
        assert await mentor.list_articles_needing_embedding("text-embedding-3-small") == []
    assert fakes.embeddings is not None
    assert len(fakes.embeddings.calls[0]) == 3  # type: ignore[attr-defined]  # все три статьи


@pytest.mark.usefixtures("mentor_enabled")
async def test_long_history_goes_into_summary(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    fakes = providers(summary="Спрашивала про встречу гостя.")
    long_text = "слово " * 900  # ≈1800 токенов по оценке «символов / 3»
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        for _ in range(2):
            await _collect(ask(user_id, long_text, providers=fakes))
        conversation = await mentor.get_conversation(user_id, mentor.ConversationMode.ASK)
        assert conversation is not None
        assert conversation.summary == "Спрашивала про встречу гостя."
        assert len(conversation.messages) == 4  # сотрудник видит всё
        assert len(conversation.history) < 4  # модель — только хвост

        await _collect(ask(user_id, "Ещё вопрос", providers=fakes))
    assert isinstance(fakes.streaming, MockLlmProvider)
    system = fakes.streaming.calls[-1].system or ""
    assert system.endswith("Резюме прошлой части разговора:\nСпрашивала про встречу гостя.")
    assert isinstance(fakes.chat, MockLlmProvider)
    summary_request = fakes.chat.calls[0]
    assert summary_request.purpose == "mentor"
    assert summary_request.messages[0].content.startswith("Новые реплики:\nСотрудник: слово")


@pytest.mark.usefixtures("mentor_enabled")
async def test_summary_failure_keeps_the_answer(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    long_text = "слово " * 900
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MAX_ATTEMPTS", "1")
        get_settings.cache_clear()
        fakes = MentorProviders(
            chat=MockLlmProvider(timeouts_before_success=5),
            streaming=MockLlmProvider(text="Ответ."),
            embeddings=providers().embeddings,
        )
        with tenant_context(tenant_id):
            await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
            for _ in range(2):
                events = await _collect(ask(user_id, long_text, providers=fakes))
                assert isinstance(events[-1], MentorDone)
            conversation = await mentor.get_conversation(user_id, mentor.ConversationMode.ASK)
    get_settings.cache_clear()
    assert conversation is not None
    assert len(conversation.messages) == 4 and conversation.summary is None
