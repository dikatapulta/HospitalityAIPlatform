"""Сквозной тест уровня вебхука для inline-кнопок персонала (spec 0021 П-2, R-7).

Регресс-барьер к блокеру ревью PR #87: `callback_query` из реального вебхука
проходит `normalize_update → process_update → insert_inbound_message`, где
`MessageContentKind(message.kind.value)` = `MessageContentKind("callback")`. Пока
в enum хранения нет значения `callback`, это ValueError → 500 → Telegram ретраит
апдейт по кругу, и ни одна кнопка на staging не срабатывает. Тесты кнопок в
test_staff.py зовут `handle_staff_message` напрямую, минуя слой хранения, поэтому
CI на этот путь зелёный — ровно тот случай, о котором R-7.

Канон оформления — test_webhook.py: настоящий `create_app`, `ASGITransport`,
фейк-отправитель. Staff-чат задаётся окружением, как в test_walking_skeleton.py.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

TEST_SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
AUTH = {SECRET_HEADER: TEST_SECRET}
STAFF_CHAT = 999
# id сообщения-уведомления, под которым висят кнопки (кнопка ≈ ответ на него).
NOTIFICATION_MESSAGE_ID = 4242


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное/кнопки/тосты."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.toasts: list[tuple[str, str]] = []
        self.keyboard_edits: list[tuple[str, str, dict[str, Any] | None]] = []

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
        self.keyboard_edits.append((chat_id, message_id, reply_markup))


def _callback_update(update_id: int, callback_data: str) -> dict[str, Any]:
    """Payload нажатия inline-кнопки под уведомлением о заявке (как шлёт Telegram)."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": 42},
            "data": callback_data,
            "message": {
                "message_id": NOTIFICATION_MESSAGE_ID,
                "chat": {"id": STAFF_CHAT},
                "text": "🔔 Новая заявка #1",
            },
        },
    }


@pytest.fixture
async def webhook(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, uuid.UUID]]:
    """Клиент вебхука с секретом и заданным staff-чатом; отправитель — фейк."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", TEST_SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", str(STAFF_CHAT))
    get_settings.cache_clear()
    app = create_app()
    sender = RecordingSender()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    # Staff-путь оркестратор не зовёт; переопределяем ради полной герметичности.
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text="unused")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, demo_tenant
    get_settings.cache_clear()


async def _make_request(tenant_id: uuid.UUID) -> uuid.UUID:
    """Создать заявку (статус NEW) у тенанта канала — под неё будут кнопки."""
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="убрать 305")
        )
    return request.id


async def test_start_button_moves_request_and_dedupes_by_update_id(
    webhook: tuple[AsyncClient, RecordingSender, uuid.UUID],
) -> None:
    """Кнопка «Взять в работу» сквозь настоящий вебхук: 200, заявка in_progress,
    тост нажавшему; повтор того же update_id — без второго перехода (P-8).

    Регресс к блокеру PR #87: до фикса `callback_query` ронял вебхук на
    `MessageContentKind("callback")` (ValueError → 500 → бесконечный ретрай).
    """
    client, sender, tenant_id = webhook
    request_id = await _make_request(tenant_id)
    data = keyboards.build_callback_data(request_id, keyboards.CallbackAction.START)

    first = await client.post(
        "/channels/telegram/webhook", json=_callback_update(50, data), headers=AUTH
    )
    assert first.status_code == 200
    with tenant_context(tenant_id):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.IN_PROGRESS
    assert len(sender.toasts) == 1 and "in_progress" in sender.toasts[0][1]

    # Повторная доставка того же update_id — no-op на слое хранения (P-8): второй
    # входящий Message не создаётся, до перехода управление не доходит → нет второго
    # тоста (это дедуп доставки, а не отбой картой переходов).
    second = await client.post(
        "/channels/telegram/webhook", json=_callback_update(50, data), headers=AUTH
    )
    assert second.status_code == 200
    assert len(sender.toasts) == 1  # второго тоста нет
    with tenant_context(tenant_id):
        assert (
            await requests_api.get_request(request_id)
        ).status is requests_api.RequestStatus.IN_PROGRESS
