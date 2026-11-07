"""«Предложить идею»: карточки, закрытые диалоги, ретеншн (spec 0071 §3, §8, §12 PR 2)."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from hospitality.modules.mentor.api import (
    ERR_MENTOR_NO_IDEA_DRAFT,
    IDEA_RETENTION_DAYS,
    ConversationMode,
    IdeaKind,
    IdeaMark,
    IdeaRead,
    MessageRole,
    NewMentorMessage,
    append_messages,
    delete_conversation,
    delete_expired_mentor_texts,
    delete_my_idea,
    get_conversation,
    get_idea_draft,
    list_my_ideas,
    list_sent_ideas,
    mark_idea_reviewed,
    save_idea_draft,
    send_idea,
)
from hospitality.modules.mentor.models import MentorConversation, MentorIdea, MentorMessage
from hospitality.modules.mentor.tests.conftest import make_user
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

ASK = ConversationMode.ASK
QUIZ = ConversationMode.QUIZ
IDEA = ConversationMode.IDEA
INTERVIEW = ConversationMode.INTERVIEW
MESSAGES_BOUNDARY = timedelta(days=90)


def _said(text: str, role: MessageRole = MessageRole.USER) -> NewMentorMessage:
    return NewMentorMessage(role=role, text=text, created_at=utc_now())


async def _drafted(user_id: uuid.UUID, text: str = "Уборка сама после выезда") -> IdeaRead:
    """Ход идеи: реплика автора и черновик модели в открытом диалоге."""
    conversation = await append_messages(user_id, IDEA, [_said(text)])
    draft = await save_idea_draft(
        user_id,
        conversation.id,
        summary=f"Идея: {text}",
        mark=IdeaMark.FEASIBLE,
        mark_note="Доска «Выезды и уборка» — рядом",
    )
    assert draft is not None
    return draft


async def _count(model: type[MentorIdea] | type[MentorConversation]) -> int:
    async with session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_ask_and_quiz_keep_one_dialog_each(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Частичная уникальность не меняет «Наставника»: диалог режима — один и не закрывается."""
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        first = await append_messages(anna, ASK, [_said("Как встречать?")])
        second = await append_messages(anna, ASK, [_said("А с ребёнком?")])
        quiz = await append_messages(anna, QUIZ, [_said("?", MessageRole.ASSISTANT)])
        assert first.id == second.id != quiz.id
        assert len(second.messages) == 2
        await _drafted(anna)
        assert await delete_conversation(anna, IDEA) is True
        assert await get_conversation(anna, ASK) is not None


async def test_draft_is_replaced_by_next_proposal(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        first = await _drafted(anna)
        conversation = await get_conversation(anna, IDEA)
        assert conversation is not None
        second = await save_idea_draft(
            anna, conversation.id, summary="Точнее", mark=IdeaMark.EXISTS, mark_note="Уже есть"
        )
        assert second is not None
        assert (second.id, second.kind, second.sent) == (first.id, IdeaKind.IDEA, False)
        assert await get_idea_draft(anna, IDEA) == second
        assert await list_my_ideas(anna) == []


async def test_sending_closes_dialog_and_next_idea_starts_a_new_one(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        draft = await _drafted(anna)
        sent = await send_idea(anna, draft.id, summary="Поправленный автором пересказ")
        assert sent.sent and sent.summary == "Поправленный автором пересказ"
        assert sent.mark is IdeaMark.FEASIBLE  # пометка модели остаётся основателю
        assert await get_conversation(anna, IDEA) is None
        assert await get_idea_draft(anna, IDEA) is None

        await _drafted(anna, "Вторая идея")
        conversation = await get_conversation(anna, IDEA)
        assert conversation is not None and conversation.id != draft.conversation_id
        assert [m.text for m in conversation.messages] == ["Вторая идея"]
        assert [idea.id for idea in await list_my_ideas(anna)] == [sent.id]


async def test_second_send_is_refused(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Вторая вкладка: черновик уже отправлен — ERR-MENTOR-005, карточка не меняется."""
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        draft = await _drafted(anna)
        await send_idea(anna, draft.id, summary="Первая вкладка")
        with pytest.raises(AppError) as error:
            await send_idea(anna, draft.id, summary="Вторая вкладка")
        assert (error.value.code, error.value.status_code) == (ERR_MENTOR_NO_IDEA_DRAFT, 409)
        [mine] = await list_my_ideas(anna)
        assert mine.summary == "Первая вкладка"

        with pytest.raises(AppError) as missing:
            await send_idea(anna, uuid.uuid4(), summary="Нет черновика")
        assert missing.value.code == ERR_MENTOR_NO_IDEA_DRAFT


async def test_draft_is_not_written_into_closed_dialog(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Модель думала, а идею отправили в другой вкладке — черновик никуда не пишется."""
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        draft = await _drafted(anna)
        await send_idea(anna, draft.id, summary="Отправлено")
        late = await save_idea_draft(anna, draft.conversation_id, summary="Опоздавший черновик")
        assert late is None
        [mine] = await list_my_ideas(anna)
        assert mine.summary == "Отправлено"
        assert await get_idea_draft(anna, IDEA) is None


async def test_start_over_keeps_sent_ideas(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        sent = await send_idea(anna, (await _drafted(anna)).id, summary="Отправлено")
        await _drafted(anna, "Незаконченная")
        assert await delete_conversation(anna, IDEA) is True
        assert await get_idea_draft(anna, IDEA) is None
        assert [idea.id for idea in await list_my_ideas(anna)] == [sent.id]
        assert await delete_conversation(anna, IDEA) is False  # открытого больше нет
        async with session_scope() as session:
            kept = await session.scalar(
                select(func.count())
                .select_from(MentorMessage)
                .where(MentorMessage.conversation_id == sent.conversation_id)
            )
        assert kept == 1


async def test_interview_summary_has_no_mark(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    boris = await make_user("Борис")
    with tenant_context(tenant_a):
        conversation = await append_messages(boris, INTERVIEW, [_said("Смена с 8 утра")])
        summary = await save_idea_draft(boris, conversation.id, summary="Итог интервью")
        assert summary is not None
        assert (summary.kind, summary.mark) == (IdeaKind.INTERVIEW, None)
        with pytest.raises(ValueError, match="no mark"):
            await save_idea_draft(boris, conversation.id, summary="Итог", mark=IdeaMark.UNKNOWN)
        # CHECK в базе — последняя линия, если вызывающий обойдёт проверку модуля.
        with pytest.raises(IntegrityError):
            async with session_scope() as session:
                await session.execute(
                    update(MentorIdea)
                    .where(MentorIdea.id == summary.id)
                    .values(mark=IdeaMark.CONSTRAINT)
                )


async def test_ask_dialog_has_no_idea_card(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        conversation = await append_messages(anna, ASK, [_said("Вопрос")])
        with pytest.raises(ValueError, match="no idea card"):
            await save_idea_draft(anna, conversation.id, summary="Не идея")


async def test_author_deletes_own_idea_with_its_dialog(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna, boris = await make_user("Анна"), await make_user("Борис")
    with tenant_context(tenant_a):
        sent = await send_idea(anna, (await _drafted(anna)).id, summary="Отправлено")
        assert await delete_my_idea(boris, sent.id) is False  # чужую — нельзя
        assert await delete_my_idea(anna, sent.id) is True
        assert await list_my_ideas(anna) == []
        assert (await _count(MentorIdea), await _count(MentorConversation)) == (0, 0)
        assert await delete_my_idea(anna, sent.id) is False


async def test_other_employee_and_other_hotel_cannot_touch_the_idea(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    anna, boris = await make_user("Анна"), await make_user("Борис")
    with tenant_context(tenant_a):
        draft = await _drafted(anna)
        with pytest.raises(AppError):
            await send_idea(boris, draft.id, summary="Чужая")
        assert await save_idea_draft(boris, draft.conversation_id, summary="Чужой") is None
        assert await get_idea_draft(boris, IDEA) is None
    with tenant_context(tenant_b):
        # Та же учётка в другом отеле (членство в двух отелях) карточек A не видит.
        assert await get_idea_draft(anna, IDEA) is None
        with pytest.raises(AppError):
            await send_idea(anna, draft.id, summary="Из другого отеля")
        assert await delete_my_idea(anna, draft.id) is False
        assert await _count(MentorIdea) == 0
    with tenant_context(tenant_a):
        assert await get_idea_draft(anna, IDEA) == draft


async def test_retention_drafts_and_messages_90_days_sent_ideas_365(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna, boris, vera = await make_user("Анна"), await make_user("Борис"), await make_user("Вера")
    now = utc_now()
    with tenant_context(tenant_a):
        stale_draft = await _drafted(anna, "Брошенный черновик")
        year_old = await send_idea(boris, (await _drafted(boris)).id, summary="Год назад")
        recent = await send_idea(vera, (await _drafted(vera)).id, summary="Полгода назад")
        async with session_scope() as session:
            await session.execute(
                update(MentorIdea)
                .where(MentorIdea.id == stale_draft.id)
                .values(updated_at=now - timedelta(days=91))
            )
            await session.execute(
                update(MentorIdea)
                .where(MentorIdea.id == year_old.id)
                .values(sent_at=now - timedelta(days=IDEA_RETENTION_DAYS + 1))
            )
            await session.execute(
                update(MentorIdea)
                .where(MentorIdea.id == recent.id)
                .values(sent_at=now - timedelta(days=180))
            )
            await session.execute(
                update(MentorMessage)
                .where(MentorMessage.conversation_id == recent.conversation_id)
                .values(created_at=now - timedelta(days=180))
            )

        stats = await delete_expired_mentor_texts(created_before=now - MESSAGES_BOUNDARY)

        assert (stats.messages_deleted, stats.ideas_deleted) == (1, 2)
        assert await get_idea_draft(anna, IDEA) is None
        assert await get_conversation(anna, IDEA) is not None  # реплики свежие — остались
        assert await list_my_ideas(boris) == []
        [kept] = await list_my_ideas(vera)
        assert kept.summary == "Полгода назад"  # карточка живёт год, её реплики — 90 дней
        again = await delete_expired_mentor_texts(created_before=now - MESSAGES_BOUNDARY)
        assert (again.messages_deleted, again.ideas_deleted) == (0, 0)


async def test_all_ideas_lists_sent_cards_of_the_hotel_with_their_dialogs(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Все идеи» (spec 0071 §8): отправленные карточки отеля всех авторов, без черновиков,
    с разговором; «Разобрано» убирает из фильтра «Не разобранные»; чужого отеля нет."""
    tenant_a, tenant_b = two_tenants
    anna, boris, gleb = await make_user("Анна"), await make_user("Борис"), await make_user("Глеб")
    with tenant_context(tenant_b):
        foreign = await send_idea(gleb, (await _drafted(gleb)).id, summary="Чужой отель")
    with tenant_context(tenant_a):
        first = await send_idea(boris, (await _drafted(boris, "Первая")).id, summary="Первая")
        second = await send_idea(anna, (await _drafted(anna, "Вторая")).id, summary="Вторая")
        await _drafted(anna, "Черновик без отправки")

        listed = await list_sent_ideas()
        assert [entry.idea.id for entry in listed] == [second.id, first.id]
        assert [m.text for m in listed[0].messages] == ["Вторая"]
        assert listed[0].idea.mark is IdeaMark.FEASIBLE

        assert await mark_idea_reviewed(first.id) is True
        [reviewed] = [e.idea for e in await list_sent_ideas() if e.idea.id == first.id]
        assert reviewed.reviewed_at is not None
        assert await mark_idea_reviewed(first.id) is True  # повтор — первая отметка остаётся
        [again] = [e.idea for e in await list_sent_ideas() if e.idea.id == first.id]
        assert again.reviewed_at == reviewed.reviewed_at
        assert [e.idea.id for e in await list_sent_ideas(unreviewed_only=True)] == [second.id]

        assert await mark_idea_reviewed(foreign.id) is False
        draft = await get_idea_draft(anna, IDEA)
        assert draft is not None and await mark_idea_reviewed(draft.id) is False
