"""Числа «пульса пользы» у владельца — `mentor_activity`, `has_active_exam_assignment`
(spec 0035 §8.1, #1050 ч. 2).

Числа разные (3/2, 2/1/1, 2 не засчитано) намеренно: на равных перестановка полей
осталась бы зелёной. Второй отель с теми же данными — проверка RLS: его числа в счёт не идут.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from hospitality.modules.mentor.api import (
    ConversationMode,
    MessageRole,
    has_active_exam_assignment,
    mentor_activity,
)
from hospitality.modules.mentor.models import (
    MentorConversation,
    MentorExamAssignment,
    MentorExamAttempt,
    MentorMessage,
)
from hospitality.modules.mentor.tests.conftest import make_user
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context


async def _turns(user_id: uuid.UUID, mode: ConversationMode, *roles: MessageRole) -> None:
    async with session_scope() as session:
        conversation = MentorConversation(user_id=user_id, mode=mode)
        session.add(conversation)
        await session.flush()
        session.add_all(
            MentorMessage(conversation_id=conversation.id, role=role, text="…") for role in roles
        )


async def _attempt(
    user_id: uuid.UUID,
    *,
    started_at: datetime,
    finished_at: datetime | None = None,
    passed: bool | None = None,
    ungraded: int = 0,
) -> None:
    async with session_scope() as session:
        session.add(
            MentorExamAttempt(
                user_id=user_id,
                question_count=5,
                pass_percent=70,
                started_at=started_at,
                finished_at=finished_at,
                percent=None if finished_at is None else 80,
                passed=passed,
                ungraded_count=ungraded,
            )
        )


async def _seed_day(anna: uuid.UUID, boris: uuid.UUID, now: datetime) -> None:
    await _turns(anna, ConversationMode.ASK, MessageRole.USER, MessageRole.ASSISTANT)
    await _turns(anna, ConversationMode.QUIZ, MessageRole.USER, MessageRole.ASSISTANT)
    await _turns(boris, ConversationMode.IDEA, MessageRole.USER)
    # Начат вчера, закончен в окне — «закончено», но не «начато».
    await _attempt(anna, started_at=now - timedelta(days=1), finished_at=now, passed=True)
    await _attempt(boris, started_at=now, finished_at=now, passed=False, ungraded=2)
    await _attempt(boris, started_at=now)


async def test_counts_turns_people_and_exams_of_the_window(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    anna, boris = await make_user("Анна"), await make_user("Борис")
    now = utc_now()
    with tenant_context(tenant_a):
        await _seed_day(anna, boris, now)
    with tenant_context(tenant_b):
        await _turns(anna, ConversationMode.ASK, MessageRole.USER)
        await _attempt(anna, started_at=now, ungraded=5)

    window = {"created_after": now - timedelta(hours=1), "created_before": now + timedelta(hours=1)}
    with tenant_context(tenant_a):
        activity = await mentor_activity(**window)
    assert activity.turns == 3  # реплики наставника не в счёт
    assert activity.people == 2
    assert (activity.exams_started, activity.exams_finished, activity.exams_passed) == (2, 2, 1)
    assert activity.ungraded == 2


async def test_window_is_half_open_and_empty_day_is_zero(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _turns(anna, ConversationMode.ASK, MessageRole.USER)
        now = utc_now()
        await _attempt(anna, started_at=now, finished_at=now, passed=True)
        # Окно кончается ровно на моменте попытки: `created_before` в окно не входит.
        before = await mentor_activity(created_after=now - timedelta(days=1), created_before=now)
        later = await mentor_activity(
            created_after=now + timedelta(seconds=1), created_before=now + timedelta(days=1)
        )
    assert (before.exams_started, before.exams_finished) == (0, 0)
    assert later.model_dump() == dict.fromkeys(later.model_dump(), 0)


async def test_active_assignment_ignores_archived_and_other_hotels(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_b):
        async with session_scope() as session:
            session.add(MentorExamAssignment(chapters=[], service_keys=[]))
    with tenant_context(tenant_a):
        assert not await has_active_exam_assignment()
        async with session_scope() as session:
            session.add(MentorExamAssignment(chapters=[], service_keys=[], archived_at=utc_now()))
        assert not await has_active_exam_assignment()
        async with session_scope() as session:
            session.add(MentorExamAssignment(chapters=[], service_keys=[]))
        assert await has_active_exam_assignment()
