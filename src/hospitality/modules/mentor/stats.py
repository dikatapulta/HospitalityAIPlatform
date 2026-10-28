"""Статистика «Тренировки» — «Где ошибаются» и «Обнулить» (spec 0048 §6).

Модуль только считает оценки; кто вправе видеть какой срез, решает кабинет.
Учётки, чьи оценки считаются, тоже передаёт кабинет (`user_ids`): модуль не знает
статусов учёток, а оценки стёртой учётки живут в таблице до «Обнулить» или
обращения субъекта (ревью PR #512, Н-17) — показывать их нельзя.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.elements import ColumnElement

from hospitality.modules.mentor.models import MentorQuizAnswer, QuizGrade
from hospitality.modules.mentor.schemas import ChapterQuizStats, UserQuizStats
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


def _count(grade: QuizGrade) -> ColumnElement[int]:
    return func.count().filter(MentorQuizAnswer.grade == grade)


_COUNTS = (_count(QuizGrade.CORRECT), _count(QuizGrade.PARTIAL), _count(QuizGrade.WRONG))


async def quiz_stats_by_chapter(*, user_ids: Collection[uuid.UUID]) -> list[ChapterQuizStats]:
    """Оценки по главам (снимок номера и названия), только учёток из `user_ids`."""
    if not user_ids:
        return []
    async with session_scope() as session:
        rows = await session.execute(
            select(MentorQuizAnswer.chapter_number, MentorQuizAnswer.chapter_title, *_COUNTS)
            .where(MentorQuizAnswer.user_id.in_(user_ids))
            .group_by(MentorQuizAnswer.chapter_number, MentorQuizAnswer.chapter_title)
            .order_by(MentorQuizAnswer.chapter_number, MentorQuizAnswer.chapter_title)
        )
        return [
            ChapterQuizStats(
                chapter_number=number,
                chapter_title=title,
                correct=correct,
                partial=partial,
                wrong=wrong,
            )
            for number, title, correct, partial, wrong in rows
        ]


async def quiz_stats_by_user(*, user_ids: Collection[uuid.UUID]) -> list[UserQuizStats]:
    """Оценки по сотрудникам — только учёток из `user_ids`; без ответов строки нет."""
    if not user_ids:
        return []
    async with session_scope() as session:
        rows = await session.execute(
            select(MentorQuizAnswer.user_id, *_COUNTS)
            .where(MentorQuizAnswer.user_id.in_(user_ids))
            .group_by(MentorQuizAnswer.user_id)
        )
        return [
            UserQuizStats(user_id=user_id, correct=correct, partial=partial, wrong=wrong)
            for user_id, correct, partial, wrong in rows
        ]


async def quiz_stats_since(*, user_ids: Collection[uuid.UUID]) -> datetime | None:
    """Момент первой учтённой оценки — «с 28.10»; оценок нет — `None`."""
    if not user_ids:
        return None
    async with session_scope() as session:
        return cast(
            "datetime | None",
            await session.scalar(
                select(func.min(MentorQuizAnswer.created_at)).where(
                    MentorQuizAnswer.user_id.in_(user_ids)
                )
            ),
        )


async def reset_quiz_stats() -> int:
    """«Обнулить статистику» (менеджер): все оценки отеля; число удалённых строк."""
    async with session_scope() as session:
        result = cast("CursorResult[Any]", await session.execute(delete(MentorQuizAnswer)))
    deleted = result.rowcount or 0
    logger.info("mentor_quiz_stats_reset", answers_deleted=deleted)
    return deleted
