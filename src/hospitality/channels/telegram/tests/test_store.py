"""Персистентность канала: идемпотентность и повторное использование диалога
(Task 0016, P-8). Проверяет контракт store-функций напрямую, без HTTP.
"""

from __future__ import annotations

import uuid

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.store import ensure_conversation, insert_inbound_message
from hospitality.shared.tenancy import tenant_context


def _inbound(update_id: int) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id="777",
        external_message_id="1",
        idempotency_key=f"telegram:update:{update_id}",
        kind=MessageKind.TEXT,
        text="hi",
    )


async def test_ensure_conversation_is_idempotent(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant):
        first = await ensure_conversation("777")
        second = await ensure_conversation("777")
    assert first == second


async def test_duplicate_delivery_key_returns_none(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("777")
        stored = await insert_inbound_message(conversation_id, _inbound(1), "corr-1")
        duplicate = await insert_inbound_message(conversation_id, _inbound(1), "corr-2")
    assert stored is not None
    assert duplicate is None
