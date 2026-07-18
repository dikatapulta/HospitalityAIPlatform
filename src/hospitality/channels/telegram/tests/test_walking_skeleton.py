"""Сквозной поток Walking Skeleton (Task 0017, DoD) на mock-провайдере.

От сообщения гостя до подтверждения о выполнении через настоящий стек: ASGI
(`create_app`) + шина событий (`deliver_pending_events`) + подписчики-уведомления.
LLM не дёргается — провайдер подменён scripted-фейком; сеть Telegram не дёргается —
отправитель подменён запоминающим фейком (один и тот же на реплики канала и на
уведомления подписчиков, чтобы все исходящие собирались в один список).

Прогон воркера — инлайновый (`deliver_pending_events()`): та же доставка outbox,
что в проде, но без отдельного процесса — тест детерминирован. Заявки читаются
только через публичный API модуля requests (внутренности его таблиц каналу
недоступны — контракт import-linter).
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
from hospitality.channels.telegram import notifications
from hospitality.channels.telegram.models import Message, MessageDirection
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.tenancy import tenant_context

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 555
STAFF_CHAT = 999


class RecordingSender:
    """Фейк-отправитель: копит (chat_id, text), возвращает фиктивный message_id."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> str | None:
        self.sent.append((chat_id, text))
        return "m-" + str(len(self.sent))


def _guest_text(update_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": GUEST_CHAT}, "text": text},
    }


def _staff_text(update_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": STAFF_CHAT}, "text": text},
    }


def _create_request_call() -> ToolCall:
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "убрать номер 305",
            "room_number": "305",
            # Вопрос-подтверждение — аргумент инструмента на языке гостя (гость
            # видит именно его; модель в проде даёт tool_use без свободного текста).
            "confirmation_question": "Оформить уборку номера 305?",
        },
    )


@pytest.fixture
async def skeleton(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID]]:
    """Стенд сквозного потока: секрет и staff-чат заданы, отправитель и провайдер —
    фейки; подписчики уведомлений зарегистрированы на том же отправителе."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", str(STAFF_CHAT))
    get_settings.cache_clear()

    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка номера")
        )

    sender = RecordingSender()
    # Двухходовой сценарий оркестратора: предложить заявку → на «да» исполнить.
    # Ход подтверждения — структурный вердикт классификатора (гейт P-9, Task 0017.1):
    # заявка создаётся из сохранённого pending_action, ре-эмиссии tool_use нет.
    provider = ScriptedLlmProvider(
        [
            MockTurn(tool_calls=[_create_request_call()]),  # без текста — вопрос в аргументе
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="toolu_verdict",
                        name="resolve_confirmation",
                        arguments={
                            "decision": "confirm",
                            "reply": "Готово, передал в службу отеля.",
                        },
                    )
                ]
            ),
        ]
    )
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider
    # Подписчики — на том же отправителе (уведомления шлёт воркер, здесь — инлайн).
    notifications.register(sender=sender, staff_chat_id=str(STAFF_CHAT))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, app, demo_tenant
    get_settings.cache_clear()


async def _requests(tenant_id: uuid.UUID) -> list[requests_api.ServiceRequestRead]:
    with tenant_context(tenant_id):
        return (await requests_api.list_requests(limit=10, offset=0)).items


async def _message_by_key(tenant_id: uuid.UUID, idempotency_key: str) -> Message:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            row = await session.scalar(
                select(Message).where(Message.idempotency_key == idempotency_key)
            )
    assert row is not None
    return row


async def test_end_to_end_guest_to_done(
    skeleton: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """Гость → AI → заявка → уведомление службе → закрытие → подтверждение гостю.

    Проверяет и то, что уведомление службе несёт correlation_id исходного сообщения
    гостя (outbox протаскивает его через async-границу доставки события)."""
    client, sender, _app, tenant_id = skeleton

    # 1. Гость просит уборку → гейт P-9: заявки ещё нет, канал переспрашивает.
    first = await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "уберите 305"), headers=AUTH
    )
    assert first.status_code == 200
    assert await _requests(tenant_id) == []
    assert sender.sent[-1] == (str(GUEST_CHAT), "Оформить уборку номера 305?")

    # 2. Гость подтверждает → заявка создана, канал отвечает, correlation_id = C.
    second = await client.post(
        "/channels/telegram/webhook", json=_guest_text(2, "да"), headers=AUTH
    )
    assert second.status_code == 200
    correlation = second.headers["X-Correlation-ID"]
    (request,) = await _requests(tenant_id)
    assert request.status is requests_api.RequestStatus.NEW
    assert (str(GUEST_CHAT), "Готово, передал в службу отеля.") in sender.sent

    # 3. Воркер доставляет request.created → уведомление в staff-чат (с id заявки).
    assert await deliver_pending_events() >= 1
    staff_msg = await _message_by_key(tenant_id, f"staff:request_created:{request.id}")
    assert staff_msg.direction is MessageDirection.OUTBOUND
    assert str(request.id) in (staff_msg.text or "")
    # DoD: уведомление службе связано с исходным сообщением гостя одним correlation_id.
    assert staff_msg.correlation_id == correlation
    staff_sends = [text for chat, text in sender.sent if chat == str(STAFF_CHAT)]
    assert len(staff_sends) == 1 and str(request.id) in staff_sends[0]

    # 4. Сотрудник ведёт заявку по жизненному циклу командами в staff-чате.
    for update_id, verb in ((3, "assign"), (4, "start"), (5, "done")):
        resp = await client.post(
            "/channels/telegram/webhook",
            json=_staff_text(update_id, f"/{verb} {request.id}"),
            headers=AUTH,
        )
        assert resp.status_code == 200
    (updated,) = await _requests(tenant_id)
    assert updated.status is requests_api.RequestStatus.DONE

    # 5. Воркер доставляет request.status_changed(done) → подтверждение гостю.
    assert await deliver_pending_events() >= 1
    guest_msg = await _message_by_key(tenant_id, f"guest:request_done:{request.id}")
    assert guest_msg.direction is MessageDirection.OUTBOUND
    assert "убрать номер 305" in (guest_msg.text or "")
    guest_confirmations = [
        text for chat, text in sender.sent if chat == str(GUEST_CHAT) and "выполнена" in text
    ]
    assert len(guest_confirmations) == 1


async def test_question_without_action_creates_no_request(
    skeleton: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """Гость задаёт вопрос (модель ответила текстом без инструмента) → заявки нет."""
    client, sender, app, tenant_id = skeleton
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(
        text="Завтрак с 7:00 до 11:00."
    )
    response = await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "во сколько завтрак?"), headers=AUTH
    )
    assert response.status_code == 200
    assert await _requests(tenant_id) == []
    assert sender.sent[-1] == (str(GUEST_CHAT), "Завтрак с 7:00 до 11:00.")


async def test_outbox_empty_after_delivery(
    skeleton: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """После создания заявки и доставки outbox пуст (событие помечено обработанным)."""
    client, _sender, _app, _tenant_id = skeleton
    await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "уберите 305"), headers=AUTH
    )
    await client.post("/channels/telegram/webhook", json=_guest_text(2, "да"), headers=AUTH)
    assert await deliver_pending_events() >= 1  # request.created доставлено
    assert await deliver_pending_events() == 0  # больше нечего доставлять
