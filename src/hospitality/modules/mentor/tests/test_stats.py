"""Статистика «Тренировки» — «Где ошибаются» и «Обнулить» (spec 0048 §6).

Изоляция `mentor_quiz_answers` между отелями — здесь (ревью PR #512, Н-3): без RLS
на этой таблице отель B увидел бы и обнулил бы оценки отеля A.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from hospitality.modules.mentor.api import (
    QuizGrade,
    quiz_stats_by_chapter,
    quiz_stats_by_user,
    quiz_stats_since,
    reset_quiz_stats,
)
from hospitality.modules.mentor.models import MentorQuizAnswer
from hospitality.modules.mentor.tests.conftest import make_user
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context


async def _grades(user_id: uuid.UUID, chapter: tuple[int, str], *grades: QuizGrade) -> None:
    async with session_scope() as session:
        session.add_all(
            MentorQuizAnswer(
                user_id=user_id,
                article_number="3",
                chapter_number=chapter[0],
                chapter_title=chapter[1],
                grade=grade,
            )
            for grade in grades
        )


async def _count() -> int:
    async with session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(MentorQuizAnswer)) or 0)


async def test_counts_by_chapter_and_by_user(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna, boris = await make_user("Анна"), await make_user("Борис")
    porter = (2, "Портье")
    with tenant_context(tenant_a):
        await _grades(anna, porter, QuizGrade.CORRECT, QuizGrade.WRONG, QuizGrade.PARTIAL)
        await _grades(boris, porter, QuizGrade.WRONG)
        await _grades(boris, (1, "О кодексе"), QuizGrade.CORRECT)

        chapters = await quiz_stats_by_chapter(user_ids={anna, boris})
        assert [(c.chapter_number, c.chapter_title) for c in chapters] == [
            (1, "О кодексе"),
            (2, "Портье"),
        ]
        assert (chapters[1].correct, chapters[1].partial, chapters[1].wrong) == (1, 1, 2)
        assert (chapters[1].total, chapters[1].errors) == (4, 3)

        users = {row.user_id: row for row in await quiz_stats_by_user(user_ids={anna, boris})}
        assert (users[anna].total, users[anna].errors) == (3, 2)
        assert (users[boris].correct, users[boris].wrong) == (1, 1)

        since = await quiz_stats_since(user_ids={anna})
        assert since is not None and utc_now() - since < timedelta(minutes=1)


async def test_only_listed_users_are_counted(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Учётку вне списка (стёртую — Н-17) не видно ни по главам, ни по людям."""
    tenant_a, _ = two_tenants
    anna, erased = await make_user("Анна"), await make_user("Стёртая")
    with tenant_context(tenant_a):
        await _grades(anna, (2, "Портье"), QuizGrade.CORRECT)
        await _grades(erased, (2, "Портье"), QuizGrade.WRONG, QuizGrade.WRONG)

        chapters = await quiz_stats_by_chapter(user_ids={anna})
        assert [(c.total, c.wrong) for c in chapters] == [(1, 0)]
        assert [row.user_id for row in await quiz_stats_by_user(user_ids={anna})] == [anna]
        assert await quiz_stats_by_chapter(user_ids=set()) == []
        assert await quiz_stats_since(user_ids=set()) is None


async def test_reset_and_other_tenant_isolation(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _grades(anna, (2, "Портье"), QuizGrade.CORRECT, QuizGrade.WRONG)
    with tenant_context(tenant_b):
        # Тот же сотрудник в другом отеле: оценок отеля A не видно и не обнулить.
        assert await quiz_stats_by_chapter(user_ids={anna}) == []
        assert await quiz_stats_by_user(user_ids={anna}) == []
        assert await reset_quiz_stats() == 0
    with tenant_context(tenant_a):
        assert await _count() == 2
        assert await reset_quiz_stats() == 2
        assert await _count() == 0
