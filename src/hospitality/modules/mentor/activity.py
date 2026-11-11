"""Пользуются ли «Наставником» — числа «пульса пользы» (spec 0035 §8.1, #1050).

Модуль только считает; сутки отеля и адресата выбирает вызывающий (канон
`count_escalations`). Окно полуоткрытое `[created_after, created_before)`, чтение —
под RLS текущего тенанта (P-4). Текстов и имён здесь нет — только счётчики.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import distinct, exists, func, select

from hospitality.modules.mentor.models import (
    MentorConversation,
    MentorExamAssignment,
    MentorExamAttempt,
    MentorMessage,
    MessageRole,
)
from hospitality.modules.mentor.schemas import MentorActivity
from hospitality.shared.db import session_scope


async def mentor_activity(*, created_after: datetime, created_before: datetime) -> MentorActivity:
    """Реплики сотрудников и экзамены текущего отеля в окне."""
    async with session_scope() as session:
        turns, people = (
            await session.execute(
                select(
                    func.count(MentorMessage.id), func.count(distinct(MentorConversation.user_id))
                )
                .join(MentorConversation, MentorConversation.id == MentorMessage.conversation_id)
                .where(
                    MentorMessage.role == MessageRole.USER,
                    MentorMessage.created_at >= created_after,
                    MentorMessage.created_at < created_before,
                )
            )
        ).one()
        started, ungraded = (
            await session.execute(
                select(
                    func.count(MentorExamAttempt.id),
                    func.coalesce(func.sum(MentorExamAttempt.ungraded_count), 0),
                ).where(
                    MentorExamAttempt.started_at >= created_after,
                    MentorExamAttempt.started_at < created_before,
                )
            )
        ).one()
        finished, passed = (
            await session.execute(
                select(
                    func.count(MentorExamAttempt.id),
                    func.count(MentorExamAttempt.id).filter(MentorExamAttempt.passed.is_(True)),
                ).where(
                    MentorExamAttempt.finished_at >= created_after,
                    MentorExamAttempt.finished_at < created_before,
                )
            )
        ).one()
    return MentorActivity(
        turns=int(turns),
        people=int(people),
        exams_started=int(started),
        exams_finished=int(finished),
        exams_passed=int(passed),
        ungraded=int(ungraded),
    )


async def has_active_exam_assignment() -> bool:
    """Есть ли у текущего отеля назначенный и не снятый экзамен (spec 0072 §3)."""
    async with session_scope() as session:
        return bool(
            await session.scalar(select(exists().where(MentorExamAssignment.archived_at.is_(None))))
        )
