"""Durable-факт эскалации (spec 0035 §6.1, issue #300) — блок «Эскалации» §13.

Проверяет два инварианта таблицы `conversation_escalations`: одна публикация —
ровно одна строка, и ретеншн гостевых текстов (spec 0032) строку не сносит.
Второе — причина, по которой связь с диалогом сделана `ON DELETE SET NULL`, а
не `CASCADE`: через 90 дней диалог исчезает, а факт «бот позвал человека» текста
гостя не содержит и переживать ретеншн вправе.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.channels.common.events import count_escalations, publish_escalation
from hospitality.channels.common.models import (
    Conversation,
    ConversationEscalation,
    Message,
    MessageContentKind,
    MessageDirection,
)
from hospitality.channels.common.retention import enforce_guest_text_retention
from hospitality.channels.common.store import ensure_conversation
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

RETENTION_DAYS = 90


async def _escalate(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    reason: EscalationReason = EscalationReason.LLM_UNAVAILABLE,
) -> None:
    """Штатный путь публикации — тот же, что зовёт ход гостя."""
    with tenant_context(tenant_id):
        await publish_escalation(
            conversation_id,
            uuid.uuid4(),
            chat_id="4242",
            guest_message="позовите человека",
            escalation=EscalationContext(reason=reason, error_code="ERR-AI-002"),
        )


async def _rows(tenant_id: uuid.UUID) -> list[ConversationEscalation]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list((await session.scalars(select(ConversationEscalation))).all())


async def test_each_escalation_writes_exactly_one_row(demo_tenant: uuid.UUID) -> None:
    """spec 0035 §6.1: строка пишется в единственном месте публикации и по одной
    на публикацию — иначе число «бот звал сотрудника» врало бы в обе стороны."""
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "4242")

    await _escalate(demo_tenant, conversation_id, reason=EscalationReason.LLM_UNAVAILABLE)
    await _escalate(demo_tenant, conversation_id, reason=EscalationReason.EMERGENCY)

    rows = await _rows(demo_tenant)
    assert len(rows) == 2
    assert {row.reason for row in rows} == {"llm_unavailable", "emergency"}
    assert {row.conversation_id for row in rows} == {conversation_id}


async def test_count_escalations_respects_the_window(demo_tenant: uuid.UUID) -> None:
    """Границы окна приходят от вызывающей стороны, окно полуоткрытое: эскалация
    ровно в полночь принадлежит наступившему дню, а не обоим сразу."""
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "4242")
    await _escalate(demo_tenant, conversation_id)
    await _escalate(demo_tenant, conversation_id)

    now = utc_now()
    with tenant_context(demo_tenant):
        # Одну из двух двигаем во вчера — окно обязано её отсечь.
        async with session_scope() as session:
            stale_id = (await session.scalars(select(ConversationEscalation.id))).first()
            await session.execute(
                update(ConversationEscalation)
                .where(ConversationEscalation.id == stale_id)
                .values(created_at=now - timedelta(days=1))
            )
        inside = await count_escalations(
            created_after=now - timedelta(hours=1), created_before=now + timedelta(hours=1)
        )
        outside = await count_escalations(
            created_after=now - timedelta(days=2), created_before=now - timedelta(hours=12)
        )

    assert inside == 1
    assert outside == 1


async def test_retention_deletes_the_conversation_but_keeps_the_fact(
    demo_tenant: uuid.UUID,
) -> None:
    """spec 0035 §6.1: `ON DELETE SET NULL`, а не `CASCADE`. Ретеншн (#42, spec
    0032) через 90 дней сносит старый диалог вместе с текстами гостя — факт
    «бот позвал человека» текста не содержит и остаётся, потеряв только ссылку.
    Иначе счёт эскалаций молча обнулялся бы каждые три месяца задним числом."""
    old = utc_now() - timedelta(days=RETENTION_DAYS + 1)
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", "4242")
        async with session_scope() as session:
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.INBOUND,
                    content_kind=MessageContentKind.TEXT,
                    text="давняя реплика",
                    correlation_id="test",
                )
            )
    await _escalate(demo_tenant, conversation_id)
    with tenant_context(demo_tenant):
        # Состарить и диалог, и его сообщение — иначе ретеншну нечего удалять.
        async with session_scope() as session:
            await session.execute(update(Message).values(created_at=old))
            await session.execute(update(Conversation).values(updated_at=old))

    await enforce_guest_text_retention(RETENTION_DAYS)

    with tenant_context(demo_tenant):
        async with session_scope() as session:
            conversations = await session.scalar(select(func.count()).select_from(Conversation))
    rows = await _rows(demo_tenant)
    assert conversations == 0  # диалог действительно снесён
    assert len(rows) == 1
    assert rows[0].conversation_id is None  # факт пережил ретеншн, ссылка обнулилась


async def test_two_tenants_do_not_see_each_others_escalations(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-4/ADR-003: таблица тенантная, RLS-блок скопирован с канона 0002. Число
    «бот звал сотрудника» — часть сводки, которую менеджер отеля видит на своей
    странице: подмешать туда соседний отель нельзя ни на строку."""
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        conversation_a = await ensure_conversation("telegram", "111")
    with tenant_context(tenant_b):
        conversation_b = await ensure_conversation("telegram", "222")
    await _escalate(tenant_a, conversation_a)
    await _escalate(tenant_a, conversation_a)
    await _escalate(tenant_b, conversation_b)

    window = {
        "created_after": utc_now() - timedelta(hours=1),
        "created_before": utc_now() + timedelta(hours=1),
    }
    with tenant_context(tenant_a):
        count_a = await count_escalations(**window)
    with tenant_context(tenant_b):
        count_b = await count_escalations(**window)

    assert (count_a, count_b) == (2, 1)
