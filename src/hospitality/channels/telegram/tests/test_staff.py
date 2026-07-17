"""Команды персонала в staff-чате (Task 0017, ADR-011).

Проверяет разбор и исполнение команд напрямую (`handle_staff_message`), без HTTP:
успешный переход, недопустимый переход, неизвестная команда, кривой id. Ответ
персоналу перехватывается фейк-отправителем.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context

STAFF_CHAT = "999"


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        return "m1"


def _command(text: str) -> NormalizedMessage:
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id="1",
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=MessageKind.TEXT,
        text=text,
    )


async def _make_request(tenant_id: uuid.UUID, key: str = "housekeeping") -> uuid.UUID:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=key, name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="убрать 305")
        )
    return request.id


async def _run(tenant_id: uuid.UUID, text: str) -> str:
    """Прогнать команду, вернуть текст ответа персоналу."""
    sender = RecordingSender()
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(STAFF_CHAT)
        await handle_staff_message(
            conversation_id, _command(text), sender=sender, correlation_id="c1"
        )
    assert len(sender.sent) == 1
    chat_id, reply = sender.sent[0]
    assert chat_id == STAFF_CHAT
    return reply


async def test_valid_transition_moves_request(demo_tenant: uuid.UUID) -> None:
    request_id = await _make_request(demo_tenant)
    reply = await _run(demo_tenant, f"/assign {request_id}")
    assert "assigned" in reply
    with tenant_context(demo_tenant):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.ASSIGNED


async def test_command_with_bot_suffix_is_accepted(demo_tenant: uuid.UUID) -> None:
    # В группах Telegram дописывает @botusername к команде — он не должен мешать.
    request_id = await _make_request(demo_tenant)
    reply = await _run(demo_tenant, f"/assign@demo_bot {request_id}")
    assert "assigned" in reply


async def test_invalid_transition_reports_error_and_keeps_status(demo_tenant: uuid.UUID) -> None:
    request_id = await _make_request(demo_tenant)  # заявка NEW: new → done запрещён
    reply = await _run(demo_tenant, f"/done {request_id}")
    assert requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION in reply
    with tenant_context(demo_tenant):
        assert (await requests_api.get_request(request_id)).status is requests_api.RequestStatus.NEW


async def test_unknown_request_reports_not_found(demo_tenant: uuid.UUID) -> None:
    reply = await _run(demo_tenant, f"/assign {uuid.uuid4()}")
    assert requests_api.ERR_REQUESTS_REQUEST_NOT_FOUND in reply


@pytest.mark.parametrize(
    "text",
    ["привет", "/frobnicate 123", "/done", "/done не-uuid"],
)
async def test_bad_command_returns_hint_not_crash(demo_tenant: uuid.UUID, text: str) -> None:
    reply = await _run(demo_tenant, text)
    assert reply  # понятная подсказка, а не исключение
