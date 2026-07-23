"""Вебхук Telegram: секрет, идемпотентность, нормализация (Task 0016, §8.4, P-8).

Канон HTTP-теста с БД (как в модуле requests): `httpx.AsyncClient` поверх ASGI
из настоящего composition root (`create_app`) — один event loop с async-фикстурами
БД. Отправитель ответов и LLM-провайдер оркестратора (Task 0017: текст теперь идёт
в оркестратор) подменяются фейками через `dependency_overrides`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.telegram.guest import UNSUPPORTED_REPLY
from hospitality.channels.telegram.models import Conversation, Message, MessageDirection
from hospitality.channels.telegram.router import (
    ERR_TELEGRAM_BAD_SECRET,
    get_orchestrator_provider,
    get_telegram_sender,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context

TEST_SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
AUTH = {SECRET_HEADER: TEST_SECRET}
CHAT_ID = 555
# Task 0017: текст идёт в оркестратор. Здесь провайдер отвечает ПРОСТЫМ текстом
# (без инструмента) — эти тесты про секрет/идемпотентность/хранение, не про заявки.
BOT_REPLY = "Здравствуйте! Чем помочь?"


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное/кнопки/тосты."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.toasts: list[tuple[str, str]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        return "999"

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        self.toasts.append((callback_id, text))

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        return None


def _text_update(update_id: int, text: str = "уберите номер 305") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": 7, "chat": {"id": CHAT_ID}, "text": text},
    }


def _photo_update(update_id: int) -> dict[str, Any]:
    # Сообщение без text — не-текст (фото); лишнее поле photo отбрасывает extra=ignore.
    return {
        "update_id": update_id,
        "message": {"message_id": 8, "chat": {"id": CHAT_ID}, "photo": [{"file_id": "x"}]},
    }


async def _stored_messages(tenant_id: uuid.UUID) -> list[Message]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(select(Message).order_by(Message.created_at))
            return list(rows)


async def _conversation_count(tenant_id: uuid.UUID) -> int:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return len((await session.scalars(select(Conversation))).all())


@pytest.fixture
async def webhook(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, uuid.UUID]]:
    """Клиент вебхука с настроенным секретом и фейк-отправителем."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()
    app = create_app()
    sender = RecordingSender()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text=BOT_REPLY)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, demo_tenant
    get_settings.cache_clear()


async def test_wrong_secret_returns_403_and_stores_nothing(
    webhook: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    client, sender, tenant_id = webhook
    for headers in ({SECRET_HEADER: "nope"}, {}):
        response = await client.post(
            "/channels/telegram/webhook", json=_text_update(1), headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ERR_TELEGRAM_BAD_SECRET
    assert await _stored_messages(tenant_id) == []
    assert sender.sent == []


async def test_text_message_stored_with_correlation_id(
    webhook: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """DoD (Task 0016): входящее — Message с correlation_id; Task 0017: текст теперь
    получает ответ AI-консьержа (мок-провайдер вернул простой текст)."""
    client, sender, tenant_id = webhook
    response = await client.post(
        "/channels/telegram/webhook", json=_text_update(10, text="привет"), headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    messages = await _stored_messages(tenant_id)
    assert [m.direction for m in messages] == [MessageDirection.INBOUND, MessageDirection.OUTBOUND]
    inbound, outbound = messages
    assert inbound.text == "привет"
    assert inbound.external_message_id == "7"
    # Прямая проверка DoD: строка связана со следом запроса в логах.
    assert inbound.correlation_id == response.headers["X-Correlation-ID"]
    assert outbound.text == BOT_REPLY
    assert await _conversation_count(tenant_id) == 1
    assert sender.sent == [(str(CHAT_ID), BOT_REPLY)]


async def test_duplicate_update_creates_single_message(
    webhook: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Идемпотентность по update_id (P-8): повтор вебхука не создаёт второй входящий
    Message и не влечёт второй ответ AI."""
    client, sender, tenant_id = webhook
    first = await client.post("/channels/telegram/webhook", json=_text_update(20), headers=AUTH)
    second = await client.post("/channels/telegram/webhook", json=_text_update(20), headers=AUTH)
    assert first.status_code == 200
    assert second.status_code == 200

    messages = await _stored_messages(tenant_id)
    inbound = [m for m in messages if m.direction is MessageDirection.INBOUND]
    assert len(inbound) == 1
    assert sender.sent == [(str(CHAT_ID), BOT_REPLY)]  # один ответ, не два
    assert await _conversation_count(tenant_id) == 1


async def test_non_text_message_gets_polite_refusal(
    webhook: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Не-текст → вежливый отказ: входящее сохранено, отказ отправлен и записан."""
    client, sender, tenant_id = webhook
    response = await client.post("/channels/telegram/webhook", json=_photo_update(30), headers=AUTH)
    assert response.status_code == 200

    messages = await _stored_messages(tenant_id)
    assert [m.direction for m in messages] == [MessageDirection.INBOUND, MessageDirection.OUTBOUND]
    inbound, outbound = messages
    assert inbound.text is None  # не-текст не разбираем в Phase 0
    assert outbound.text == UNSUPPORTED_REPLY
    assert sender.sent == [(str(CHAT_ID), UNSUPPORTED_REPLY)]


async def test_non_message_update_is_noop(
    webhook: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Обновление без message (edited_message и т.п.) — 200 без побочных эффектов."""
    client, sender, tenant_id = webhook
    response = await client.post(
        "/channels/telegram/webhook",
        json={"update_id": 40, "edited_message": {"message_id": 1, "chat": {"id": CHAT_ID}}},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert await _stored_messages(tenant_id) == []
    assert await _conversation_count(tenant_id) == 0
    assert sender.sent == []


async def test_empty_secret_config_is_fail_closed(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пустой TELEGRAM_WEBHOOK_SECRET отвергает всё (§11: закрыто по умолчанию)."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/channels/telegram/webhook", json=_text_update(1), headers={SECRET_HEADER: ""}
        )
    get_settings.cache_clear()
    assert response.status_code == 403
    assert response.json()["error"]["code"] == ERR_TELEGRAM_BAD_SECRET
