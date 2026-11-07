"""«Предложить идею» и интервью служб — карточки (spec 0071 §3, §8).

Разговор идеи — обычный диалог «Наставника» режима `idea` или `interview`
(`dialogs.py`): реплики пишет `append_messages`, «Начать заново» —
`delete_conversation`. Здесь — карточка этого диалога: черновик из предложения
модели, отправка (закрывает диалог), «Мои идеи», удаление автором и ретеншн.
Модуль о модели не знает — черновик приходит готовым из `ai/`. Каждый писатель
сначала берёт `FOR UPDATE` строки диалога: две вкладки одного автора не
отправят карточку дважды и не перепишут уже отправленную.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.mentor.models import (
    ConversationMode,
    IdeaKind,
    IdeaMark,
    MentorConversation,
    MentorIdea,
)
from hospitality.modules.mentor.schemas import IdeaRead
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_MENTOR_NO_IDEA_DRAFT: Final = "ERR-MENTOR-005"

# Отправленная карточка живёт год от `sent_at` (spec 0071 §8, решение сессии);
# черновик и реплики — срок текстов «Наставника» (граница вызывающего).
IDEA_RETENTION_DAYS: Final = 365

_KIND_BY_MODE: Final = {
    ConversationMode.IDEA: IdeaKind.IDEA,
    ConversationMode.INTERVIEW: IdeaKind.INTERVIEW,
}


async def save_idea_draft(
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    summary: str,
    mark: IdeaMark | None = None,
    mark_note: str | None = None,
) -> IdeaRead | None:
    """Черновик карточки из предложения модели; прежний черновик диалога заменяется.

    Диалог `conversation_id` — тот, что прочитал ход. Он уже не открыт (идею
    отправили в другой вкладке, «Начать заново» стёрло его, пока думала модель) —
    `None`, черновик не пишется ни в него, ни в новый диалог режима (как резюме, #531).
    У итога интервью пометки нет (spec 0071 §4) — пометка к нему — ошибка вызывающего.
    """
    async with session_scope() as session:
        conversation = await _lock_open(session, user_id, conversation_id)
        if conversation is None:
            logger.info("mentor_idea_draft_skipped", reason="conversation_not_open")
            return None
        kind = _KIND_BY_MODE.get(conversation.mode)
        if kind is None:
            raise ValueError(f"mentor mode {conversation.mode.value!r} has no idea card")
        if kind is IdeaKind.INTERVIEW and (mark is not None or mark_note is not None):
            raise ValueError("interview summary carries no mark (spec 0071 §4)")
        idea = await session.scalar(
            select(MentorIdea).where(MentorIdea.conversation_id == conversation.id)
        )
        if idea is None:
            idea = MentorIdea(user_id=user_id, conversation_id=conversation.id, kind=kind)
            session.add(idea)
        idea.summary = summary
        idea.mark = mark
        idea.mark_note = mark_note
        idea.updated_at = utc_now()
        await session.flush()
        result = IdeaRead.model_validate(idea)
    logger.info(
        "mentor_idea_draft_saved",
        idea_id=str(result.id),
        kind=kind.value,
        mark=mark and mark.value,
    )
    return result


async def get_idea_draft(user_id: uuid.UUID, mode: ConversationMode) -> IdeaRead | None:
    """Черновик открытого диалога режима; нет диалога или предложения — `None`."""
    async with session_scope() as session:
        idea = await session.scalar(
            select(MentorIdea)
            .join(MentorConversation, MentorConversation.id == MentorIdea.conversation_id)
            .where(
                MentorConversation.user_id == user_id,
                MentorConversation.mode == mode,
                MentorConversation.closed_at.is_(None),
            )
        )
        return None if idea is None else IdeaRead.model_validate(idea)


async def send_idea(user_id: uuid.UUID, idea_id: uuid.UUID, *, summary: str) -> IdeaRead:
    """«Отправить разработчику» (spec 0071 §3 п. 4): пересказ — из поля автора, диалог закрыт.

    Черновика нет, он чужой или уже отправлен (вторая вкладка, повтор формы) —
    ERR-MENTOR-005 (409): карточка не меняется.
    """
    async with session_scope() as session:
        conversation_id = await session.scalar(
            select(MentorIdea.conversation_id).where(
                MentorIdea.id == idea_id, MentorIdea.user_id == user_id
            )
        )
        conversation = (
            None if conversation_id is None else await _lock_open(session, user_id, conversation_id)
        )
        idea = (
            None
            if conversation is None
            else await session.scalar(select(MentorIdea).where(MentorIdea.id == idea_id))
        )
        if conversation is None or idea is None or idea.sent_at is not None:
            raise AppError(
                code=ERR_MENTOR_NO_IDEA_DRAFT,
                message="Idea draft is gone or already sent: reload the page",
                status_code=409,
            )
        now = utc_now()
        idea.summary = summary
        idea.sent_at = now
        idea.updated_at = now
        conversation.closed_at = now
        await session.flush()
        result = IdeaRead.model_validate(idea)
    logger.info("mentor_idea_sent", idea_id=str(result.id), kind=result.kind.value)
    return result


async def list_my_ideas(user_id: uuid.UUID) -> list[IdeaRead]:
    """«Мои идеи»: отправленные карточки автора, свежие сверху (spec 0071 §8)."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorIdea)
            .where(MentorIdea.user_id == user_id, MentorIdea.sent_at.is_not(None))
            .order_by(MentorIdea.sent_at.desc())
        )
        return [IdeaRead.model_validate(row) for row in rows]


async def delete_my_idea(user_id: uuid.UUID, idea_id: uuid.UUID) -> bool:
    """«Удалить» в «Моих идеях»: карточка вместе с её разговором — разработчик больше
    не увидит ни того, ни другого. Чужая или уже удалённая — `False`."""
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(MentorConversation).where(
                    MentorConversation.user_id == user_id,
                    MentorConversation.id
                    == select(MentorIdea.conversation_id)
                    .where(MentorIdea.id == idea_id, MentorIdea.user_id == user_id)
                    .scalar_subquery(),
                )
            ),
        )
    deleted = bool(result.rowcount)
    if deleted:
        logger.info("mentor_idea_deleted", idea_id=str(idea_id))
    return deleted


async def delete_expired_ideas(session: AsyncSession, *, drafts_before: datetime) -> int:
    """Ретеншн карточек (spec 0071 §8) внутри прохода `delete_expired_mentor_texts`.

    Черновик — по своему `updated_at` и границе реплик; отправленная — через
    `IDEA_RETENTION_DAYS` от `sent_at`, вместе с закрытым диалогом (его реплики
    к тому времени уже удалены своим сроком). Число удалённых карточек.
    """
    drafts = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(MentorIdea).where(
                MentorIdea.sent_at.is_(None), MentorIdea.updated_at < drafts_before
            )
        ),
    )
    sent_before = utc_now() - timedelta(days=IDEA_RETENTION_DAYS)
    sent = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(MentorConversation).where(
                MentorConversation.id.in_(
                    select(MentorIdea.conversation_id).where(MentorIdea.sent_at < sent_before)
                )
            )
        ),
    )
    return (drafts.rowcount or 0) + (sent.rowcount or 0)


async def _lock_open(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> MentorConversation | None:
    conversation: MentorConversation | None = await session.scalar(
        select(MentorConversation)
        .where(
            MentorConversation.id == conversation_id,
            MentorConversation.user_id == user_id,
            MentorConversation.closed_at.is_(None),
        )
        .with_for_update()
    )
    return conversation
