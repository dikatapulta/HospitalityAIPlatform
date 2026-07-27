"""Эскалация к человеку доходит до staff-чата (spec 0022, issue #36, DoD).

Сквозные сценарии на каноне walking-skeleton: ASGI (`create_app`) + шина событий
(`deliver_pending_events` — та же доставка outbox, что в проде, но инлайн) +
подписчики-уведомления на одном запоминающем отправителе. Плюс юнит-тесты
подписчика (канон `test_notifications.py`): повторная доставка события и
ненастроенный staff-чат.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.escalation import EscalationReason
from hospitality.ai.gateway.api import MockLlmProvider, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.app import create_app
from hospitality.channels.common.events import ConversationEscalated
from hospitality.channels.common.models import Message, MessageDirection
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.telegram import notifications
from hospitality.channels.telegram.notifications import notify_staff_on_conversation_escalated
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import set_staff_routing
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.tenancy import tenant_context

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 555
STAFF_CHAT = 999


class RecordingSender:
    """Фейк-отправитель (порт TelegramSender): копит отправленное."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

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
        return None


def _guest_text(update_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": GUEST_CHAT}, "text": text},
    }


def _laundry_call() -> ToolCall:
    # Категорий у тенанта в этих тестах НЕТ: исполнение упадёт с ERR-AI-004 —
    # ровно сценарий DoD «инструмент упал» (категорию удалили/не настроили).
    return ToolCall(
        id="toolu_1",
        name="create_service_request",
        arguments={
            "category_key": "laundry",
            "summary": "постирать рубашку",
            "room_number": "305",
            "confirmation_question": "Оформить стирку рубашки?",
        },
    )


def _confirm_verdict() -> ToolCall:
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": "confirm", "reply": "Готово, передаю в службу."},
    )


@pytest.fixture
async def escalation_stand(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID]]:
    """Стенд: секрет и staff-чат заданы, отправитель — фейк, подписчики
    зарегистрированы; LLM-провайдер задаёт каждый тест под свой сценарий."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", str(STAFF_CHAT))
    get_settings.cache_clear()

    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    notifications.register(
        sender=sender,
        default_staff_chat_id=str(STAFF_CHAT),
        translate_provider=MockLlmProvider(text="перевод не нужен"),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, sender, app, demo_tenant
    get_settings.cache_clear()


def _staff_escalations(sender: RecordingSender) -> list[str]:
    return [text for chat, text in sender.sent if chat == str(STAFF_CHAT) and "🆘" in text]


async def _inbound_message_id(tenant_id: uuid.UUID, text: str) -> uuid.UUID:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            message_id = await session.scalar(
                select(Message.id).where(
                    Message.direction == MessageDirection.INBOUND, Message.text == text
                )
            )
    assert message_id is not None
    return message_id


async def _message_by_key(tenant_id: uuid.UUID, idempotency_key: str) -> Message:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            row = await session.scalar(
                select(Message).where(Message.idempotency_key == idempotency_key)
            )
    assert row is not None
    return row


async def test_tool_failure_escalates_to_staff_chat(
    escalation_stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD #36: «инструмент упал» → staff-чат получает контекст, обещание правдиво.

    Категории «laundry» у тенанта нет → на подтверждении исполнение падает
    (ERR-AI-004) → NEEDS_HUMAN. Персонал видит чат, комнату, суть (последняя
    реплика гостя — «да», без сути она бесполезна) и причину; `Message`
    уведомления связан с вебхуком гостя одним correlation_id (§10.2)."""
    client, sender, app, tenant_id = escalation_stand
    # Один инстанс на весь тест: сценарий тянется через оба вебхука (канон
    # walking-skeleton); `lambda: ScriptedLlmProvider(...)` начинал бы его заново.
    provider = ScriptedLlmProvider(
        [MockTurn(tool_calls=[_laundry_call()]), MockTurn(tool_calls=[_confirm_verdict()])]
    )
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    first = await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "постирайте рубашку"), headers=AUTH
    )
    assert first.status_code == 200
    assert sender.sent[-1] == (str(GUEST_CHAT), "Оформить стирку рубашки?")

    second = await client.post(
        "/channels/telegram/webhook", json=_guest_text(2, "да"), headers=AUTH
    )
    assert second.status_code == 200
    # Гостю пообещали человека — дальше обещание обязано сбыться.
    assert "подключу сотрудника" in sender.sent[-1][1]

    assert await deliver_pending_events() >= 1
    (staff_text,) = _staff_escalations(sender)
    assert f"Чат: {GUEST_CHAT}" in staff_text
    assert "Комната: 305" in staff_text
    assert "Просьба: постирать рубашку" in staff_text
    assert "Последняя реплика: «да»" in staff_text
    assert "Бот не смог оформить заявку." in staff_text

    inbound_id = await _inbound_message_id(tenant_id, "да")
    staff_msg = await _message_by_key(tenant_id, f"staff:escalated:{inbound_id}")
    assert staff_msg.direction is MessageDirection.OUTBOUND
    assert staff_msg.correlation_id == second.headers["X-Correlation-ID"]


async def test_llm_degradation_escalates_to_staff_chat(
    escalation_stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD #36: деградация §7.8 (LLM недоступен) → staff-чат узнаёт о госте.

    Все попытки провайдера таймаутят (ERR-AI-001) → гость получает честный
    `DEGRADED_REPLY`, а эскалация уже закоммичена в outbox (без LLM в пути)."""
    client, sender, app, tenant_id = escalation_stand
    provider = MockLlmProvider(timeouts_before_success=get_settings().llm_max_attempts)
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    response = await client.post(
        "/channels/telegram/webhook", json=_guest_text(1, "мне нужна помощь"), headers=AUTH
    )
    assert response.status_code == 200
    assert "зову сотрудника" in sender.sent[-1][1]

    assert await deliver_pending_events() >= 1
    (staff_text,) = _staff_escalations(sender)
    assert f"Чат: {GUEST_CHAT}" in staff_text
    assert "Комната: —" in staff_text  # комната неизвестна — честный прочерк
    assert "Последняя реплика: «мне нужна помощь»" in staff_text
    assert "ИИ-консьерж недоступен" in staff_text

    inbound_id = await _inbound_message_id(tenant_id, "мне нужна помощь")
    staff_msg = await _message_by_key(tenant_id, f"staff:escalated:{inbound_id}")
    assert staff_msg.correlation_id == response.headers["X-Correlation-ID"]


async def test_duplicate_webhook_yields_single_escalation(
    escalation_stand: tuple[AsyncClient, RecordingSender, FastAPI, uuid.UUID],
) -> None:
    """DoD #36: дубликат вебхука → ровно одно уведомление (P-8, эшелон входа).

    Повтор `update_id` гасится дедупом входящего до публикации события —
    второй эскалации не рождается ни в outbox, ни в staff-чате."""
    client, sender, app, _tenant_id = escalation_stand
    provider = MockLlmProvider(timeouts_before_success=get_settings().llm_max_attempts)
    app.dependency_overrides[get_orchestrator_provider] = lambda: provider

    update = _guest_text(7, "помогите")
    assert (
        await client.post("/channels/telegram/webhook", json=update, headers=AUTH)
    ).status_code == 200
    assert (
        await client.post("/channels/telegram/webhook", json=update, headers=AUTH)
    ).status_code == 200

    assert await deliver_pending_events() == 1  # одно событие, не два
    assert await deliver_pending_events() == 0
    assert len(_staff_escalations(sender)) == 1


async def test_event_redelivery_sends_single_notification(demo_tenant: uuid.UUID) -> None:
    """P-8, эшелон эффекта: повторная доставка события (at-least-once, ADR-005)
    не шлёт второе сообщение — дубль гасится ключом `staff:escalated:<id>`."""
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("telegram", str(GUEST_CHAT))
        event = ConversationEscalated(
            conversation_id=conversation_id,
            inbound_message_id=uuid.uuid4(),
            chat_id=str(GUEST_CHAT),
            guest_message="мне нужна помощь",
            reason=EscalationReason.LLM_UNAVAILABLE,
            error_code="ERR-AI-001",
        )
        for _ in range(2):
            await notify_staff_on_conversation_escalated(
                event, sender=sender, default_staff_chat_id=str(STAFF_CHAT)
            )
    assert len(sender.sent) == 1


async def test_unconfigured_staff_chat_skips_without_retry(demo_tenant: uuid.UUID) -> None:
    """Пустой TELEGRAM_STAFF_CHAT_ID: warning ERR-TELEGRAM-002 и выход без
    исключения — ретрай бессмыслен, конфигурация от повтора не появится."""
    sender = RecordingSender()
    with tenant_context(demo_tenant):
        event = ConversationEscalated(
            conversation_id=uuid.uuid4(),
            inbound_message_id=uuid.uuid4(),
            chat_id=str(GUEST_CHAT),
            guest_message="помогите",
            reason=EscalationReason.UNKNOWN_TOOL,
            error_code="ERR-AI-004",
        )
        await notify_staff_on_conversation_escalated(event, sender=sender, default_staff_chat_id="")
    assert sender.sent == []


async def test_escalation_ignores_service_routing(demo_tenant: uuid.UUID) -> None:
    """spec 0026: маршрутизация по службам эскалации не касается — она всегда в
    дефолтном чате («уровень выше»). Категории у эскалации нет и быть не может:
    при llm_unavailable классификация как раз и не состоялась."""
    sender = RecordingSender()
    await set_staff_routing(demo_tenant, {"housekeeping": "-1001"})
    with tenant_context(demo_tenant):
        event = ConversationEscalated(
            conversation_id=await ensure_conversation("telegram", str(GUEST_CHAT)),
            inbound_message_id=uuid.uuid4(),
            chat_id=str(GUEST_CHAT),
            guest_message="уберите номер",  # текст «про уборку» роли не играет
            reason=EscalationReason.LLM_UNAVAILABLE,
            error_code="ERR-AI-001",
        )
        await notify_staff_on_conversation_escalated(
            event, sender=sender, default_staff_chat_id=str(STAFF_CHAT)
        )
    assert [chat for chat, _ in sender.sent] == [str(STAFF_CHAT)]


def test_every_escalation_reason_has_staff_text() -> None:
    """Каждая причина эскалации имеет строку для персонала: рассинхрон enum ↔
    словарь не должен доезжать до прода (там его прикрывает .get-резерв, но
    резерв — оборона, а не норма)."""
    assert set(notifications._ESCALATION_REASON_TEXTS) == set(EscalationReason)
