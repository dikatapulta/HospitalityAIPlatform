"""«Тренировка» (spec 0048 §5.5, §11): вопрос по статье, проверка, разбор оценки."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.mentor.codex_upload import upload_codex
from hospitality.ai.mentor.common import MentorDone, MentorEvent
from hospitality.ai.mentor.quiz import QuizTopic, check_quiz_answer, new_quiz_question, parse_grade
from hospitality.ai.mentor.tests.conftest import codex_files, providers
from hospitality.modules.mentor import api as mentor
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

QUESTION = "Гость подходит, пока вы говорите по телефону. Что вы сделаете?"


async def _done(events: AsyncGenerator[MentorEvent, None]) -> MentorDone:
    *_, last = [event async for event in events]
    assert isinstance(last, MentorDone)
    return last


async def _quiz_answers() -> list[tuple[uuid.UUID, str, str, str]]:
    """Строки статистики — SQL'ом: читать их будет кабинет (PR 4), API чтения пока нет."""
    async with session_scope() as session:
        rows = await session.execute(
            text("SELECT user_id, article_number, chapter_title, grade FROM mentor_quiz_answers")
        )
        return [(row[0], row[1], row[2], row[3]) for row in rows]


@pytest.mark.parametrize(
    ("verdict", "grade"),
    [
        ("Оценка: верно\nВсё так.", mentor.QuizGrade.CORRECT),
        ("Оценка: частично верно\nНе хватило.", mentor.QuizGrade.PARTIAL),
        ("Оценка: неверно\nНет.", mentor.QuizGrade.WRONG),
        ("**Оценка:** Частично верно", mentor.QuizGrade.PARTIAL),
        ("Хороший ответ. Оценка: верно", None),
        ("", None),
    ],
)
def test_parse_grade(verdict: str, grade: mentor.QuizGrade | None) -> None:
    assert parse_grade(verdict) == grade


@pytest.mark.usefixtures("mentor_enabled")
async def test_question_then_check_counts_the_grade(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=providers())
        asking = providers(answer=QUESTION)
        done = await _done(
            new_quiz_question(user_id, QuizTopic(chapter_number=2), providers=asking)
        )
        assert (done.answer, done.sources) == (QUESTION, [])  # источник выдал бы ответ
        assert isinstance(asking.streaming, MockLlmProvider)
        [request] = asking.streaming.calls
        # В главе 2 тренировочная (от 250 знаков) — только статья 2.
        assert request.messages[0].content.startswith("[Статья 2] Глава 2. Администратор")
        assert request.purpose == "mentor"
        assert await mentor.active_quiz_question(user_id) == ("2", QUESTION)

        checking = providers(answer="Оценка: частично верно\nНе хватило приветствия [Статья 2].")
        done = await _done(check_quiz_answer(user_id, "Закончу звонок.", providers=checking))
        assert done.answer.startswith("Оценка: частично верно")
        assert [(s.number, s.anchor) for s in done.sources] == [("2", "st-2")]
        assert isinstance(checking.streaming, MockLlmProvider)
        payload = json.loads(checking.streaming.calls[0].messages[0].content)
        assert payload["вопрос"] == QUESTION
        assert payload["ответ_сотрудника"] == "Закончу звонок."

        answers = await _quiz_answers()
        assert answers == [(user_id, "2", "Администратор", "partial")]
        conversation = await mentor.get_conversation(user_id, mentor.ConversationMode.QUIZ)
        assert conversation is not None
        assert [m.text for m in conversation.messages] == [
            QUESTION,
            "Закончу звонок.",
            done.answer,
        ]
        with pytest.raises(AppError):
            await mentor.active_quiz_question(user_id)  # вопрос снят


@pytest.mark.usefixtures("mentor_enabled")
async def test_verdict_without_grade_is_shown_but_not_counted(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=providers())
        await _done(new_quiz_question(user_id, QuizTopic(), providers=providers(answer=QUESTION)))
        done = await _done(
            check_quiz_answer(user_id, "Ответ", providers=providers(answer="Хорошо, но мало."))
        )
        assert done.answer == "Хорошо, но мало."
        assert await _quiz_answers() == []


@pytest.mark.usefixtures("mentor_enabled")
async def test_check_without_question_is_an_error(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        with pytest.raises(AppError) as error:
            await _done(check_quiz_answer(user_id, "Ответ", providers=fakes))
    assert error.value.code == mentor.ERR_MENTOR_NO_QUIZ_QUESTION
    assert isinstance(fakes.streaming, MockLlmProvider) and fakes.streaming.calls == []


@pytest.mark.usefixtures("mentor_enabled")
async def test_topic_text_is_searched(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    fakes = providers(answer=QUESTION)
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        await _done(new_quiz_question(user_id, QuizTopic(text=" встреча гостя "), providers=fakes))
    assert fakes.embeddings is not None
    assert fakes.embeddings.calls[-1] == ["встреча гостя"]  # type: ignore[attr-defined]
