"""Сбой обработки не оставляет человека в тишине — канал Telegram (issue #43).

Входящее коммитится до обработки, повтор апдейта гасит дедуп (P-8): 500 в
ответ на упавший ход ничего не чинит. Гость получает реплику деградации и
эскалацию, сотрудник — «проверьте статус заявки» (аудит 05.08, Б-5); вебхук
отвечает 200. Стенд — канон `test_escalation.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.common.guest_turn import DEGRADED_REPLY
from hospitality.channels.telegram import notifications
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.service import (
    ERR_TELEGRAM_STAFF_COMMAND_FAILED,
    STAFF_COMMAND_FAILED_REPLY,
)
from hospitality.channels.telegram.tests.conftest import RecordingSender, grant_consent
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.test_logging import find_record, read_log_records

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = "555"
STAFF_CHAT = "999"


@pytest.fixture
async def stand(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, uuid.UUID]]:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", STAFF_CHAT)
    get_settings.cache_clear()
    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text="ок")
    notifications.register(
        sender=sender,
        default_staff_chat_id=STAFF_CHAT,
        translate_provider=MockLlmProvider(text="перевод не нужен"),
    )
    await grant_consent(demo_tenant, int(GUEST_CHAT))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, demo_tenant
    get_settings.cache_clear()


def _update(update_id: int, chat_id: str, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id}, "text": text},
    }


def _to(sender: RecordingSender, chat_id: str) -> list[str]:
    return [text for chat, text in sender.sent if chat == chat_id]


async def test_crashed_guest_turn_answers_and_escalates(
    stand: tuple[AsyncClient, RecordingSender, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DoD #43: неожиданная ошибка хода → гостю реплика деградации, staff-чату
    эскалация; повтор того же апдейта второго ответа не даёт."""
    client, sender, _tenant_id = stand

    async def crash(**_: Any) -> None:
        raise RuntimeError("unexpected bug in a tool")

    monkeypatch.setattr(orchestrator, "handle_message", crash)
    configure_logging()
    update = _update(1, GUEST_CHAT, "мне нужна помощь")

    first = await client.post("/channels/telegram/webhook", json=update, headers=AUTH)
    repeat = await client.post("/channels/telegram/webhook", json=update, headers=AUTH)

    assert first.status_code == repeat.status_code == 200
    assert _to(sender, GUEST_CHAT) == [DEGRADED_REPLY]
    assert await deliver_pending_events() == 1
    (staff_text,) = _to(sender, STAFF_CHAT)
    assert "Последняя реплика: «мне нужна помощь»" in staff_text
    assert "ИИ-консьерж недоступен" in staff_text
    assert find_record(read_log_records(capsys), "guest_turn_failed")["level"] == "error"


async def test_crashed_staff_command_tells_staff_to_check(
    stand: tuple[AsyncClient, RecordingSender, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Аудит Б-5: «Готово» при мигнувшей БД — сотрудник узнаёт о сбое, вебхук 200."""
    client, sender, tenant_id = stand
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="полотенца",
            )
        )

    async def db_blip(*_: Any, **__: Any) -> None:
        raise RuntimeError("db blip")

    monkeypatch.setattr(requests_api, "change_request_status", db_blip)
    configure_logging()

    response = await client.post(
        "/channels/telegram/webhook",
        json=_update(2, STAFF_CHAT, f"/start {request.id}"),
        headers=AUTH,
    )

    assert response.status_code == 200
    assert _to(sender, STAFF_CHAT) == [STAFF_COMMAND_FAILED_REPLY]
    record = find_record(read_log_records(capsys), "staff_command_failed")
    assert record["error_code"] == ERR_TELEGRAM_STAFF_COMMAND_FAILED
    assert record["level"] == "error"
