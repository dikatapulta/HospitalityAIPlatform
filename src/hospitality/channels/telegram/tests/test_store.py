"""Персистентность канала: идемпотентность и повторное использование диалога
(Task 0016, P-8) + состояние диалога и привязки сквозной сборки (Task 0017).
Проверяет контракт store-функций напрямую, без HTTP.
"""

from __future__ import annotations

import uuid

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.models import MessageDirection
from hospitality.channels.telegram.store import (
    MAX_HISTORY_MESSAGES,
    ensure_conversation,
    insert_inbound_message,
    load_conversation_external_id,
    load_dialog_history,
    load_pending_action,
    load_request_origin_conversation,
    notification_already_sent,
    record_outbound_message,
    record_request_origin,
    set_pending_action,
)
from hospitality.shared.tenancy import tenant_context


def _inbound(update_id: int, *, text: str = "hi") -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id="777",
        external_message_id="1",
        idempotency_key=f"telegram:update:{update_id}",
        kind=MessageKind.TEXT,
        text=text,
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


async def test_pending_action_round_trip(demo_tenant: uuid.UUID) -> None:
    """Гейт P-9 переживает ход: записали → прочитали → очистили (Task 0017)."""
    action = {"tool_name": "create_service_request", "arguments": {"category_key": "housekeeping"}}
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("777")
        assert await load_pending_action(conversation_id) is None  # по умолчанию нет
        await set_pending_action(conversation_id, action)
        assert await load_pending_action(conversation_id) == action
        await set_pending_action(conversation_id, None)
        assert await load_pending_action(conversation_id) is None


async def test_request_origin_idempotent(demo_tenant: uuid.UUID) -> None:
    """Привязка заявка→диалог идемпотентна; читается обратно (Task 0017, ADR-011)."""
    request_id = uuid.uuid4()
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("777")
        await record_request_origin(request_id, conversation_id)
        await record_request_origin(request_id, conversation_id)  # повтор — не падает
        found = await load_request_origin_conversation(request_id)
        missing = await load_request_origin_conversation(uuid.uuid4())
    assert found == conversation_id
    assert missing is None


async def test_load_dialog_history_excludes_current_and_nontext(demo_tenant: uuid.UUID) -> None:
    """История для оркестратора: прежние текстовые реплики, без текущего и не-текста."""
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("777")
        first_id = await insert_inbound_message(conversation_id, _inbound(1, text="уберите"), "c1")
        await record_outbound_message(conversation_id, "оформить?", "c1", external_message_id="a")
        photo = NormalizedMessage(
            channel="telegram",
            chat_id="777",
            external_message_id="2",
            idempotency_key="telegram:update:2",
            kind=MessageKind.UNSUPPORTED,  # не-текст: в историю не попадает
        )
        await insert_inbound_message(conversation_id, photo, "c2")
        current_id = await insert_inbound_message(conversation_id, _inbound(3, text="да"), "c3")
        assert current_id is not None
        history = await load_dialog_history(conversation_id, exclude_message_id=current_id)

    assert history == [
        (MessageDirection.INBOUND, "уберите"),
        (MessageDirection.OUTBOUND, "оформить?"),
    ]
    assert first_id is not None  # прежнее входящее в истории есть, текущее — исключено


async def test_load_dialog_history_windows_to_last_n(demo_tenant: uuid.UUID) -> None:
    """Окно истории (баг #71): длинный диалог обрезается до последних N реплик,
    в хронологическом порядке — чтобы модель не имитировала давние ошибки и ход
    не рос в цене без предела."""
    total = MAX_HISTORY_MESSAGES + 5
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("777")
        for n in range(1, total + 1):
            await insert_inbound_message(conversation_id, _inbound(n, text=f"m{n}"), f"c{n}")
        history = await load_dialog_history(conversation_id, exclude_message_id=uuid.uuid4())

    assert len(history) == MAX_HISTORY_MESSAGES  # не вся история, а хвост
    # Вернулись последние N (m6..m30) в хронологическом порядке, старые отброшены.
    assert history[0] == (MessageDirection.INBOUND, f"m{total - MAX_HISTORY_MESSAGES + 1}")
    assert history[-1] == (MessageDirection.INBOUND, f"m{total}")


async def test_notification_already_sent(demo_tenant: uuid.UUID) -> None:
    """Дедуп подписчиков (P-8): ключ уже записанного исходящего распознаётся."""
    key = "staff:request_created:abc"
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("staff-chat")
        assert await load_conversation_external_id(conversation_id) == "staff-chat"
        assert await notification_already_sent(key) is False
        await record_outbound_message(
            conversation_id, "уведомление", "c1", external_message_id="x", idempotency_key=key
        )
        assert await notification_already_sent(key) is True
