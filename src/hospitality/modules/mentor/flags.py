"""«Неточность в статье» (spec 0072 §6, вариант (б)): отметка сотрудника без текста.

Сотрудник отмечает статью у «Кодекса» или под проверкой ответа и снимает отметку тем же
нажатием; «Где ошибаются» показывает по статье только число отметивших. Кто отметил,
модуль наружу не отдаёт — сотруднику его собственные отметки, кабинету счётчик. Учётки,
чьи отметки считаются, передаёт кабинет (`user_ids`), как у `stats.py`: стёртую
суперадмином учётку модуль не отличит. Новая редакция кодекса снимает отметки статей,
у которых сменился текст (`drop_stale_flags` в транзакции `service.replace_codex`).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.mentor.models import MentorArticleFlag, MentorCodexArticle
from hospitality.modules.mentor.schemas import ArticleFlagCount, CodexArticleDraft
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def set_article_flag(user_id: uuid.UUID, article_number: str, *, flagged: bool) -> bool:
    """Отметить статью (`flagged=True`) или снять отметку; ответ — стоит ли она теперь.

    Повтор не меняет ничего. Статьи нет в нынешнем кодексе (перезагружен, пока страница
    была открыта) — отметка не пишется, ответ `False`.
    """
    async with session_scope() as session:
        if not flagged:
            await session.execute(
                delete(MentorArticleFlag).where(
                    MentorArticleFlag.user_id == user_id,
                    MentorArticleFlag.article_number == article_number,
                )
            )
            return False
        exists = await session.scalar(
            select(MentorCodexArticle.id).where(MentorCodexArticle.number == article_number)
        )
        if exists is None:
            return False
        await session.execute(
            insert(MentorArticleFlag)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                article_number=article_number,
                created_at=utc_now(),
            )
            .on_conflict_do_nothing(
                constraint="uq_mentor_article_flags_tenant_id_user_id_article_number"
            )
        )
    return True


async def my_article_flags(user_id: uuid.UUID) -> set[str]:
    """Номера статей, которые отметил сотрудник, — для вида кнопок на его страницах."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorArticleFlag.article_number).where(MentorArticleFlag.user_id == user_id)
        )
        return set(rows)


async def article_flag_counts(*, user_ids: Collection[uuid.UUID]) -> list[ArticleFlagCount]:
    """Статьи нынешнего кодекса с отметками учёток из `user_ids`: больше отметок — выше,
    при равенстве — по порядку кодекса."""
    if not user_ids:
        return []
    count = func.count(MentorArticleFlag.id)
    async with session_scope() as session:
        rows = await session.execute(
            select(MentorCodexArticle.number, MentorCodexArticle.title, count)
            .join(MentorArticleFlag, MentorArticleFlag.article_number == MentorCodexArticle.number)
            .where(MentorArticleFlag.user_id.in_(user_ids))
            .group_by(
                MentorCodexArticle.number, MentorCodexArticle.title, MentorCodexArticle.position
            )
            .order_by(count.desc(), MentorCodexArticle.position)
        )
        return [
            ArticleFlagCount(article_number=number, title=title, count=flags)
            for number, title, flags in rows
        ]


async def drop_stale_flags(session: AsyncSession, drafts: Sequence[CodexArticleDraft]) -> int:
    """Снять отметки статей, чей текст сменила новая редакция или которых в ней нет.

    Зовётся загрузкой кодекса до замены статей, в её транзакции. Статья та же, если у неё
    тот же номер, название, текст и источник: неточность в ней не исправлена.
    """
    old = {
        number: (title, body, source)
        for number, title, body, source in await session.execute(
            select(
                MentorCodexArticle.number,
                MentorCodexArticle.title,
                MentorCodexArticle.body,
                MentorCodexArticle.source,
            )
        )
    }
    same = [d.number for d in drafts if old.get(d.number) == (d.title, d.body, d.source)]
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(MentorArticleFlag).where(MentorArticleFlag.article_number.not_in(same))
        ),
    )
    dropped = result.rowcount or 0
    if dropped:
        logger.info("mentor_article_flags_dropped", flags_dropped=dropped)
    return dropped
