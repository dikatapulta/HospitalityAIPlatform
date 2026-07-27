"""DoD issue #40 (spec 0025) через полный стек канала: статус и отмена «моих заявок».

Тот же стенд, что walking skeleton (ASGI + scripted-провайдер + инлайновая
доставка outbox): гость спрашивает статус — модель отвечает из снапшота
открытых заявок ДИАЛОГА (один вызов LLM, без инструментов и персонала); гость
отменяет — гейт P-9 → заявка `cancelled`, staff-чат видит отмену, гость НЕ
получает «пришлось отменить». Изоляция: чужая заявка (другой диалог того же
тенанта) в снапшот не попадает, а подделанный tool_call с её id — эскалация,
не отмена.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.gateway.api import MockLlmProvider, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.app import create_app
from hospitality.channels.common.models import Message
from hospitality.channels.common.store import ensure_conversation, record_request_origin
from hospitality.channels.telegram import notifications
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.tenancy import tenant_context

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 777
OTHER_GUEST_CHAT = 778
STAFF_CHAT = 888


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное/кнопки."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keyboard_edits: list[tuple[str, str, dict[str, Any] | None]] = []

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.sent.append((chat_id, text))
        return "m-" + str(len(self.sent))

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        return None

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        self.keyboard_edits.append((chat_id, message_id, reply_markup))


def _guest_text(update_id: int, text: str, *, chat: int = GUEST_CHAT) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat}, "text": text},
    }


def _cancel_call(request_id: uuid.UUID) -> ToolCall:
    return ToolCall(
        id="toolu_cancel",
        name="cancel_service_request",
        arguments={
            "request_id": str(request_id),
            "confirmation_question": "Отменить заявку на полотенца?",
        },
    )


def _confirm_verdict(reply: str = "Отменил вашу заявку.") -> ToolCall:
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": "confirm", "reply": reply},
    )


@pytest.fixture
async def stand(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID]]:
    """Стенд DoD #40: секрет и staff-чат заданы, отправитель — фейк; провайдер
    оркестратора задаёт каждый тест через dependency_overrides."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", str(STAFF_CHAT))
    get_settings.cache_clear()

    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )

    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    notifications.register(
        sender=sender,
        default_staff_chat_id=str(STAFF_CHAT),
        translate_provider=MockLlmProvider(text="полотенца в 305"),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, app, demo_tenant
    get_settings.cache_clear()


async def _request_from_chat(
    tenant_id: uuid.UUID, chat: int, summary: str
) -> requests_api.ServiceRequestRead:
    """Заявка, привязанная к диалогу чата, — как её оставляет создание через AI
    (заявка + `request_origins`), без прогона сценария создания."""
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", str(chat))
        categories = await requests_api.list_categories()
        category_id = next(c.id for c in categories if c.key == "housekeeping")
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id, summary=summary, room_number="305"
            )
        )
        await record_request_origin(request.id, conversation_id)
        return request


async def _message_by_key(tenant_id: uuid.UUID, idempotency_key: str) -> Message | None:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            row: Message | None = await session.scalar(
                select(Message).where(Message.idempotency_key == idempotency_key)
            )
            return row


async def test_status_question_is_answered_from_snapshot(
    stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD: «Где моя заявка?» → ответ со статусом без участия персонала.

    Модель видит снапшот открытых заявок диалога в системном промпте и отвечает
    ОДНИМ вызовом LLM, без инструментов; заявок и эскалаций не прибавилось."""
    client, sender, app, tenant_id = stand
    request = await _request_from_chat(tenant_id, GUEST_CHAT, "полотенца в 305")
    provider = ScriptedLlmProvider(
        [MockTurn(text="Ваша заявка «полотенца» уже у службы — статус: принята.")]
    )
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    response = await client.post(
        "/channels/telegram/webhook",
        json=_guest_text(1, "ну что там с полотенцами?"),
        headers=AUTH,
    )
    assert response.status_code == 200
    assert sender.sent[-1] == (
        str(GUEST_CHAT),
        "Ваша заявка «полотенца» уже у службы — статус: принята.",
    )
    # Один вызов LLM: статус — из контекста, а не из второго round-trip'а.
    assert len(provider.calls) == 1
    system = provider.calls[0].system or ""
    assert "# Active service requests in this conversation" in system
    assert str(request.id) in system
    assert "полотенца в 305" in system
    # Инструмент отмены предложен с enum ровно из заявок диалога (§7.4).
    cancel_specs = [t for t in provider.calls[0].tools if t.name == "cancel_service_request"]
    assert len(cancel_specs) == 1
    assert cancel_specs[0].input_schema["properties"]["request_id"]["enum"] == [str(request.id)]


async def test_snapshot_contains_only_own_dialog_requests(
    stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD: «чужую заявку не увидеть» — заявка другого диалога ТОГО ЖЕ тенанта
    в снапшот и в enum инструмента отмены не попадает."""
    client, _sender, app, tenant_id = stand
    own = await _request_from_chat(tenant_id, GUEST_CHAT, "полотенца в 305")
    foreign = await _request_from_chat(tenant_id, OTHER_GUEST_CHAT, "тапочки в 407")
    provider = ScriptedLlmProvider([MockTurn(text="Ваша заявка одна: полотенца.")])
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    response = await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "что с моими заявками?"), headers=AUTH
    )
    assert response.status_code == 200
    system = provider.calls[0].system or ""
    assert str(own.id) in system
    assert str(foreign.id) not in system
    assert "тапочки" not in system
    (cancel_spec,) = [t for t in provider.calls[0].tools if t.name == "cancel_service_request"]
    assert cancel_spec.input_schema["properties"]["request_id"]["enum"] == [str(own.id)]


async def test_guest_cancels_own_request_and_staff_sees_it(
    stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD: «Отмените» → подтверждение → `cancelled`; служба видит отмену через
    существующее событие `request.status_changed`; гость НЕ получает
    «к сожалению, пришлось отменить» на собственную отмену."""
    client, sender, app, tenant_id = stand
    request = await _request_from_chat(tenant_id, GUEST_CHAT, "полотенца в 305")
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_cancel_call(request.id)]),
            MockTurn(tool_calls=[_confirm_verdict()]),
        ]
    )
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    # 1. «Отмените» → гейт P-9: вопрос-подтверждение, заявка ещё жива.
    first = await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "отмените, уже не надо"), headers=AUTH
    )
    assert first.status_code == 200
    assert sender.sent[-1] == (str(GUEST_CHAT), "Отменить заявку на полотенца?")
    with tenant_context(tenant_id):
        assert (await requests_api.get_request(request.id)).status is requests_api.RequestStatus.NEW

    # 2. «Да» → отмена исполнена из сохранённого pending_action.
    second = await client.post(
        "/channels/telegram/webhook", json=_guest_text(2, "да"), headers=AUTH
    )
    assert second.status_code == 200
    assert sender.sent[-1] == (str(GUEST_CHAT), "Отменил вашу заявку.")
    with tenant_context(tenant_id):
        cancelled = await requests_api.get_request(request.id)
    assert cancelled.status is requests_api.RequestStatus.CANCELLED
    assert cancelled.resolution_note == "Отменена гостем через чат."

    # 3. Воркер доставляет request.status_changed → staff-чат видит отмену…
    assert await deliver_pending_events() >= 1
    staff_msg = await _message_by_key(tenant_id, f"staff:request_cancelled_by_guest:{request.id}")
    assert staff_msg is not None
    assert "Гость отменил заявку" in (staff_msg.text or "")
    assert f"#{cancelled.daily_number}" in (staff_msg.text or "")
    # …а гостю уведомление о ЕГО отмене не уходит (реплику модели он уже получил).
    assert await _message_by_key(tenant_id, f"guest:request_cancelled:{request.id}") is None
    guest_texts = [text for chat, text in sender.sent if chat == str(GUEST_CHAT)]
    assert not any("пришлось отменить" in text for text in guest_texts)


async def test_forged_cancel_of_foreign_request_escalates_not_cancels(
    stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD: «чужую заявку не отменить» — даже если модель (или подделанный
    pending_action) назвала id заявки другого диалога, исполнение отвергает его
    по снапшоту ТЕКУЩЕГО хода: эскалация к человеку, заявка не тронута."""
    client, sender, app, tenant_id = stand
    foreign = await _request_from_chat(tenant_id, OTHER_GUEST_CHAT, "тапочки в 407")
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_cancel_call(foreign.id)]),  # фейк «галлюцинирует» чужой id
            MockTurn(tool_calls=[_confirm_verdict("")]),
        ]
    )
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "отмените мою заявку"), headers=AUTH
    )
    second = await client.post(
        "/channels/telegram/webhook", json=_guest_text(2, "да"), headers=AUTH
    )
    assert second.status_code == 200
    with tenant_context(tenant_id):
        untouched = await requests_api.get_request(foreign.id)
    assert untouched.status is requests_api.RequestStatus.NEW  # не отменена
    # Гость получил честную эскалацию, и она дошла бы до staff-чата (spec 0022).
    assert "сотрудника" in sender.sent[-1][1]
    assert await deliver_pending_events() >= 1
    staff_texts = [text for chat, text in sender.sent if chat == str(STAFF_CHAT)]
    assert any("Гостю нужен сотрудник" in text for text in staff_texts)
