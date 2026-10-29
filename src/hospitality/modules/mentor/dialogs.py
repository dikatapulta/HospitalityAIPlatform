"""Диалоги сотрудника, вопрос «Тренировки» и оценки (spec 0048 §4–5).

Диалог — один на `(user_id, mode)`; создаётся при первой записи (`INSERT … ON
CONFLICT DO NOTHING`, затем `FOR UPDATE`), так что две вкладки одного сотрудника не
плодят второй. Реплики пишутся только после конца потока ответа (§5.4): вызывающий
передаёт их готовыми. Диалог читает только его хозяин — `user_id` в каждом запросе;
права (кто вправе видеть статистику) режет кабинет.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.mentor.models import (
    ConversationMode,
    MentorConversation,
    MentorMessage,
    MentorQuizAnswer,
    MessageRole,
    QuizGrade,
)
from hospitality.modules.mentor.schemas import (
    CodexArticleRead,
    ConversationRead,
    MentorMessageRead,
    MentorRetentionStats,
    NewMentorMessage,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_MENTOR_NO_QUIZ_QUESTION = "ERR-MENTOR-003"

# Сколько последних статей «Тренировка» не повторяет (spec 0048 §5).
QUIZ_NO_REPEAT: Final = 6


async def get_conversation(user_id: uuid.UUID, mode: ConversationMode) -> ConversationRead | None:
    """Диалог сотрудника в режиме со всеми репликами по порядку; `None` — его нет."""
    async with session_scope() as session:
        conversation = await session.scalar(_conversation_query(user_id, mode))
        if conversation is None:
            return None
        return await _read(session, conversation)


async def append_messages(
    user_id: uuid.UUID, mode: ConversationMode, messages: Sequence[NewMentorMessage]
) -> ConversationRead:
    """Дописать реплики в диалог (создав его) и вернуть диалог целиком."""
    async with session_scope() as session:
        conversation = await _lock_conversation(session, user_id, mode)
        _add_messages(session, conversation, messages)
        await session.flush()
        return await _read(session, conversation)


async def compact_history(
    user_id: uuid.UUID,
    mode: ConversationMode,
    *,
    message_ids: Sequence[uuid.UUID],
    summary: str,
) -> None:
    """Пересказать старые реплики в резюме: оно заменяет прежнее, реплики помечаются.

    Реплики остаются у сотрудника в диалоге до срока ретеншна, но в историю для
    модели больше не идут (`ConversationRead.history`). Диалога нет — сотрудник
    нажал «Начать заново», пока модель писала резюме, — резюме не записывается:
    новый диалог с ним помнил бы стёртый разговор (§5.6, #531).
    """
    async with session_scope() as session:
        conversation = await session.scalar(_conversation_query(user_id, mode).with_for_update())
        if conversation is None:
            logger.info("mentor_summary_skipped", reason="conversation_reset")
            return
        conversation.summary = summary
        conversation.summary_updated_at = utc_now()
        await session.execute(
            update(MentorMessage)
            .where(
                MentorMessage.conversation_id == conversation.id,
                MentorMessage.id.in_(message_ids),
            )
            .values(in_summary=True)
        )


async def start_quiz_question(
    user_id: uuid.UUID, *, article_number: str, question: NewMentorMessage
) -> None:
    """Запомнить вопрос «Тренировки» и его статью; статья уходит в недавние."""
    async with session_scope() as session:
        conversation = await _lock_conversation(session, user_id, ConversationMode.QUIZ)
        conversation.quiz_article_number = article_number
        conversation.quiz_question = question.text
        recent = [n for n in conversation.quiz_recent_articles if n != article_number]
        conversation.quiz_recent_articles = [*recent, article_number][-QUIZ_NO_REPEAT:]
        _add_messages(session, conversation, [question])


async def active_quiz_question(user_id: uuid.UUID) -> tuple[str, str]:
    """`(номер статьи, текст вопроса)` текущего вопроса; нет вопроса — ERR-MENTOR-003."""
    conversation = await get_conversation(user_id, ConversationMode.QUIZ)
    if (
        conversation is None
        or conversation.quiz_article_number is None
        or conversation.quiz_question is None
    ):
        raise _no_quiz_question()
    return conversation.quiz_article_number, conversation.quiz_question


async def finish_quiz_answer(
    user_id: uuid.UUID,
    *,
    article: CodexArticleRead,
    answer: NewMentorMessage,
    verdict: NewMentorMessage,
    grade: QuizGrade | None,
) -> None:
    """Записать ответ и проверку, снять вопрос, засчитать оценку (если она разобрана).

    Вопрос сменился, пока шла проверка (новый вопрос в другой вкладке), —
    ERR-MENTOR-003: оценка относилась бы не к тому вопросу.
    """
    async with session_scope() as session:
        conversation = await _lock_conversation(session, user_id, ConversationMode.QUIZ)
        if conversation.quiz_article_number != article.number:
            raise _no_quiz_question()
        conversation.quiz_article_number = None
        conversation.quiz_question = None
        _add_messages(session, conversation, [answer, verdict])
        if grade is not None:
            session.add(
                MentorQuizAnswer(
                    user_id=user_id,
                    article_number=article.number,
                    chapter_number=article.chapter_number,
                    chapter_title=article.chapter_title,
                    grade=grade,
                )
            )
    logger.info("mentor_quiz_answered", article_number=article.number, grade=grade and grade.value)


async def delete_conversation(user_id: uuid.UUID, mode: ConversationMode) -> bool:
    """«Начать заново» (spec 0048 §5.6): диалог режима целиком; `False` — его не было."""
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(MentorConversation).where(
                    MentorConversation.user_id == user_id, MentorConversation.mode == mode
                )
            ),
        )
    return bool(result.rowcount)


async def delete_expired_mentor_texts(*, created_before: datetime) -> MentorRetentionStats:
    """Ретеншн (spec 0048 §4): реплики и резюме старше границы. Идемпотентна.

    Срок тот же, что у текстов гостя (spec 0032), — зовёт тот же проход воркера.
    Оценки «Тренировки» текста не содержат и живут, пока живёт учётка.
    """
    async with session_scope() as session:
        deleted = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(MentorMessage).where(MentorMessage.created_at < created_before)
            ),
        )
        cleared = cast(
            "CursorResult[Any]",
            await session.execute(
                update(MentorConversation)
                .where(MentorConversation.summary_updated_at < created_before)
                .values(summary=None, summary_updated_at=None)
            ),
        )
    return MentorRetentionStats(
        messages_deleted=deleted.rowcount or 0, summaries_cleared=cleared.rowcount or 0
    )


def _conversation_query(user_id: uuid.UUID, mode: ConversationMode) -> Any:
    return select(MentorConversation).where(
        MentorConversation.user_id == user_id, MentorConversation.mode == mode
    )


async def _lock_conversation(
    session: AsyncSession, user_id: uuid.UUID, mode: ConversationMode
) -> MentorConversation:
    now = utc_now()
    await session.execute(
        insert(MentorConversation)
        .values(id=uuid.uuid4(), user_id=user_id, mode=mode, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["tenant_id", "user_id", "mode"])
    )
    conversation = await session.scalar(_conversation_query(user_id, mode).with_for_update())
    if conversation is None:  # pragma: no cover — строку только что вставили или она была
        raise RuntimeError("mentor conversation vanished under lock")
    return cast(MentorConversation, conversation)


def _add_messages(
    session: AsyncSession, conversation: MentorConversation, messages: Sequence[NewMentorMessage]
) -> None:
    session.add_all(
        MentorMessage(
            conversation_id=conversation.id,
            role=message.role,
            text=message.text,
            cited_articles=list(message.cited_articles),
            created_at=message.created_at,
        )
        for message in messages
    )
    conversation.updated_at = utc_now()


async def _read(session: AsyncSession, conversation: MentorConversation) -> ConversationRead:
    rows = await session.scalars(
        select(MentorMessage)
        .where(MentorMessage.conversation_id == conversation.id)
        # Вопрос и ответ одного хода могут совпасть по времени до микросекунды:
        # тогда вопрос сотрудника — первым.
        .order_by(MentorMessage.created_at, MentorMessage.role != MessageRole.USER)
    )
    return ConversationRead(
        id=conversation.id,
        mode=conversation.mode,
        summary=conversation.summary,
        quiz_article_number=conversation.quiz_article_number,
        quiz_question=conversation.quiz_question,
        quiz_recent_articles=list(conversation.quiz_recent_articles),
        messages=[MentorMessageRead.model_validate(row) for row in rows],
    )


def _no_quiz_question() -> AppError:
    return AppError(
        code=ERR_MENTOR_NO_QUIZ_QUESTION,
        message="No active training question: ask for a new one",
        status_code=409,
    )
