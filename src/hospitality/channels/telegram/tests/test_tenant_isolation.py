"""Изоляция тенантов на таблицах канала Telegram (Task 0016, P-4, ADR-003).

Канон требует тест изоляции на КАЖДОЙ новой тенантной таблице: `conversations`
и `messages`. Те же проверки, что у канарейки (`tests/test_tenant_isolation.py`)
и модуля requests, но на таблицах канала — и на уровне store-функций.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.models import (
    Conversation,
    Message,
    MessageContentKind,
    MessageDirection,
)
from hospitality.channels.telegram.store import ensure_conversation, insert_inbound_message
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.tenancy import tenant_context


def _inbound(update_id: int, text: str, chat_id: str = "100") -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=chat_id,
        external_message_id="1",
        idempotency_key=f"telegram:update:{update_id}",
        kind=MessageKind.TEXT,
        text=text,
    )


async def _visible_message_texts() -> set[str]:
    async with session_scope() as session:
        rows = (await session.execute(select(Message.text))).scalars().all()
    return {row for row in rows if row is not None}


async def test_tenant_sees_only_own_conversations_and_messages(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    # Один и тот же chat_id и update_id у обоих тенантов: ограничения тенантные,
    # коллизии нет — и это же демонстрирует изоляцию.
    with tenant_context(tenant_a):
        conv_a = await ensure_conversation("100")
        await insert_inbound_message(conv_a, _inbound(1, "a-msg"), "corr-a")
    with tenant_context(tenant_b):
        conv_b = await ensure_conversation("100")
        await insert_inbound_message(conv_b, _inbound(1, "b-msg"), "corr-b")

    assert conv_a != conv_b
    with tenant_context(tenant_a):
        assert await _visible_message_texts() == {"a-msg"}
    with tenant_context(tenant_b):
        assert await _visible_message_texts() == {"b-msg"}


async def test_insert_with_foreign_tenant_id_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """WITH CHECK политики: подлог чужого tenant_id отвергает БД, не дисциплина."""
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        conversation_id = await ensure_conversation("200")

    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(Conversation(tenant_id=tenant_b, channel="telegram", external_id="stolen"))
            await session.flush()

    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(
                Message(
                    tenant_id=tenant_b,
                    conversation_id=conversation_id,
                    direction=MessageDirection.INBOUND,
                    content_kind=MessageContentKind.TEXT,
                    text="stolen",
                    correlation_id="corr",
                )
            )
            await session.flush()


async def test_platform_scope_cannot_read_channel_tables(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Платформенная сессия (без контекста тенанта) не видит таблиц канала."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        conversation_id = await ensure_conversation("300")
        await insert_inbound_message(conversation_id, _inbound(3, "hidden"), "corr")

    async with platform_session_scope() as session:
        conversations = await session.scalar(select(func.count()).select_from(Conversation))
        messages = await session.scalar(select(func.count()).select_from(Message))
    assert conversations == 0
    assert messages == 0
