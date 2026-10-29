"""Диалоги, вопрос «Тренировки», оценки и ретеншн (spec 0048 §4–5)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.mentor.api import (
    ERR_MENTOR_NO_QUIZ_QUESTION,
    QUIZ_NO_REPEAT,
    ConversationMode,
    MessageRole,
    NewMentorMessage,
    QuizGrade,
    active_quiz_question,
    append_messages,
    compact_history,
    delete_conversation,
    delete_expired_mentor_texts,
    finish_quiz_answer,
    get_articles,
    get_conversation,
    replace_codex,
    start_quiz_question,
)
from hospitality.modules.mentor.models import MentorConversation, MentorQuizAnswer
from hospitality.modules.mentor.tests.conftest import codex_files, make_user
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

ASK = ConversationMode.ASK
QUIZ = ConversationMode.QUIZ


def _message(role: MessageRole, text: str, *, cited: list[str] | None = None) -> NewMentorMessage:
    return NewMentorMessage(role=role, text=text, cited_articles=cited or [], created_at=utc_now())


async def test_exchange_is_stored_in_order_for_its_owner_only(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna, boris = await make_user("Анна"), await make_user("Борис")
    with tenant_context(tenant_a):
        same_moment = utc_now()
        # Ответ передан первым и с тем же временем — порядок всё равно «вопрос, ответ».
        await append_messages(
            anna,
            ASK,
            [
                NewMentorMessage(
                    role=MessageRole.ASSISTANT,
                    text="Встаньте [Статья 1].",
                    cited_articles=["1"],
                    created_at=same_moment,
                ),
                NewMentorMessage(
                    role=MessageRole.USER, text="Как встречать?", created_at=same_moment
                ),
            ],
        )
        conversation = await append_messages(
            anna, ASK, [_message(MessageRole.USER, "А если гость с ребёнком?")]
        )
        assert [m.text for m in conversation.messages] == [
            "Как встречать?",
            "Встаньте [Статья 1].",
            "А если гость с ребёнком?",
        ]
        assert conversation.messages[1].cited_articles == ["1"]

        # Чужой диалог сотрудник не читает: у Бориса и режима «Тренировка» — пусто.
        assert await get_conversation(boris, ASK) is None
        assert await get_conversation(anna, QUIZ) is None


async def test_compacted_messages_leave_history_but_stay_visible(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        conversation = await append_messages(
            anna,
            ASK,
            [
                _message(MessageRole.USER, "Первый вопрос"),
                _message(MessageRole.ASSISTANT, "Первый ответ"),
                _message(MessageRole.USER, "Второй вопрос"),
                _message(MessageRole.ASSISTANT, "Второй ответ"),
            ],
        )
        old = [m.id for m in conversation.messages[:2]]
        await compact_history(anna, ASK, message_ids=old, summary="Спрашивала про встречу.")
        conversation = await get_conversation(anna, ASK)  # type: ignore[assignment]
        assert conversation is not None
        assert conversation.summary == "Спрашивала про встречу."
        assert [m.text for m in conversation.history] == ["Второй вопрос", "Второй ответ"]
        assert len(conversation.messages) == 4


async def test_summary_after_start_over_does_not_bring_dialog_back(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Начать заново», пока модель писала резюме: стёртый разговор не возвращается (#531)."""
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        conversation = await append_messages(anna, ASK, [_message(MessageRole.USER, "Вопрос")])
        assert await delete_conversation(anna, ASK) is True
        await compact_history(
            anna, ASK, message_ids=[conversation.messages[0].id], summary="Спрашивала про шум."
        )
        assert await get_conversation(anna, ASK) is None


async def test_quiz_question_answer_and_grade(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await replace_codex(
            codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
        article = (await get_articles(["3"]))["3"]
        with pytest.raises(AppError) as error:
            await active_quiz_question(anna)
        assert error.value.code == ERR_MENTOR_NO_QUIZ_QUESTION

        await start_quiz_question(
            anna, article_number="3", question=_message(MessageRole.ASSISTANT, "Что вы сделаете?")
        )
        assert await active_quiz_question(anna) == ("3", "Что вы сделаете?")

        await finish_quiz_answer(
            anna,
            article=article,
            answer=_message(MessageRole.USER, "Извинюсь и отвечу."),
            verdict=_message(MessageRole.ASSISTANT, "Оценка: верно\nВсё так.", cited=["3"]),
            grade=QuizGrade.CORRECT,
        )
        with pytest.raises(AppError):
            await active_quiz_question(anna)  # вопрос снят
        conversation = await get_conversation(anna, QUIZ)
        assert conversation is not None
        assert [m.role for m in conversation.messages] == [
            MessageRole.ASSISTANT,
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]
        async with session_scope() as session:
            answers = list(await session.scalars(select(MentorQuizAnswer)))
        assert [(a.user_id, a.article_number, a.chapter_title, a.grade) for a in answers] == [
            (anna, "3", "Портье", QuizGrade.CORRECT)
        ]


async def test_quiz_without_grade_and_changed_question(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await replace_codex(
            codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
        articles = await get_articles(["2-1", "3"])
        await start_quiz_question(
            anna, article_number="3", question=_message(MessageRole.ASSISTANT, "Вопрос?")
        )
        # Оценки в ответе модели нет — в статистику не идёт, вопрос всё равно снят.
        await finish_quiz_answer(
            anna,
            article=articles["3"],
            answer=_message(MessageRole.USER, "Ответ"),
            verdict=_message(MessageRole.ASSISTANT, "Хороший ответ."),
            grade=None,
        )
        async with session_scope() as session:
            assert list(await session.scalars(select(MentorQuizAnswer))) == []

        # Пока шла проверка, в другой вкладке взяли новый вопрос — оценка не засчитывается.
        await start_quiz_question(
            anna, article_number="2-1", question=_message(MessageRole.ASSISTANT, "Другой?")
        )
        with pytest.raises(AppError) as error:
            await finish_quiz_answer(
                anna,
                article=articles["3"],
                answer=_message(MessageRole.USER, "Ответ"),
                verdict=_message(MessageRole.ASSISTANT, "Оценка: верно"),
                grade=QuizGrade.CORRECT,
            )
        assert error.value.code == ERR_MENTOR_NO_QUIZ_QUESTION
        assert await active_quiz_question(anna) == ("2-1", "Другой?")


async def test_recent_quiz_articles_are_capped(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        for number in ["1", "2", "3", "1", "4", "5", "6", "7"]:
            await start_quiz_question(
                anna, article_number=number, question=_message(MessageRole.ASSISTANT, "?")
            )
        conversation = await get_conversation(anna, QUIZ)
        assert conversation is not None
        assert (
            conversation.quiz_recent_articles
            == ["2", "3", "1", "4", "5", "6", "7"][-QUIZ_NO_REPEAT:]
        )


async def test_start_over_deletes_only_that_mode(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await append_messages(anna, ASK, [_message(MessageRole.USER, "Вопрос")])
        await start_quiz_question(
            anna, article_number="3", question=_message(MessageRole.ASSISTANT, "?")
        )
        assert await delete_conversation(anna, ASK) is True
        assert await delete_conversation(anna, ASK) is False
        assert await get_conversation(anna, ASK) is None
        assert await get_conversation(anna, QUIZ) is not None


async def test_retention_deletes_old_texts_and_summaries(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    long_ago = utc_now() - timedelta(days=100)
    with tenant_context(tenant_a):
        old = NewMentorMessage(role=MessageRole.USER, text="Старый вопрос", created_at=long_ago)
        conversation = await append_messages(
            anna, ASK, [old, _message(MessageRole.USER, "Свежий вопрос")]
        )
        await compact_history(
            anna, ASK, message_ids=[conversation.messages[0].id], summary="Старое резюме"
        )
        async with session_scope() as session:
            row = await session.scalar(select(MentorConversation))
            assert row is not None
            row.summary_updated_at = long_ago

        stats = await delete_expired_mentor_texts(created_before=utc_now() - timedelta(days=90))
        assert (stats.messages_deleted, stats.summaries_cleared) == (1, 1)
        conversation = await get_conversation(anna, ASK)  # type: ignore[assignment]
        assert conversation is not None
        assert [m.text for m in conversation.messages] == ["Свежий вопрос"]
        assert conversation.summary is None

        again = await delete_expired_mentor_texts(created_before=utc_now() - timedelta(days=90))
        assert (again.messages_deleted, again.summaries_cleared) == (0, 0)


async def test_other_tenant_sees_no_dialogs(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await append_messages(anna, ASK, [_message(MessageRole.USER, "Вопрос")])
        await start_quiz_question(
            anna, article_number="3", question=_message(MessageRole.ASSISTANT, "?")
        )
    with tenant_context(tenant_b):
        # Тот же сотрудник в другом отеле (членство в двух отелях) — диалогов A не видит.
        assert await get_conversation(anna, ASK) is None
        async with session_scope() as session:
            assert list(await session.scalars(select(MentorConversation))) == []
        stats = await delete_expired_mentor_texts(created_before=utc_now() + timedelta(days=1))
        assert stats.messages_deleted == 0
    with tenant_context(tenant_a):
        assert await get_conversation(anna, ASK) is not None
