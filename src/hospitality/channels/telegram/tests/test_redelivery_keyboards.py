"""Сообщение с клавиатурой переживает сбой Bot API (#296).

Экран согласия (spec 0029 §4) и вопрос персоналу с ForceReply (spec 0021 П-4)
шлются каноном `send_reply`: сбой первой попытки уводит их в дожим вместе с
клавиатурой, и дожим доставляет их с ней же. Устройство самого дожима — ключи,
срок годности, история диалога — проверяет `test_redelivery.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.common.consent import CONSENT_VERSION
from hospitality.channels.common.models import Message, MessageDirection
from hospitality.channels.common.store import ensure_conversation
from hospitality.channels.telegram import keyboards, redelivery
from hospitality.channels.telegram.consent import consent_keyboard, consent_screen_text
from hospitality.channels.telegram.outbound import ReplyDelivery, ReplyOutcome, send_reply
from hospitality.channels.telegram.redelivery import TelegramReplyUndelivered, redeliver_reply
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.tests.conftest import RecordingSender, grant_consent
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.events import OutboxEvent, deliver_pending_events
from hospitality.shared.tenancy import tenant_context

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 555
STAFF_CHAT = "999"
FORCE_REPLY = {"force_reply": True}
RETRY_TOAST = "Не смог задать вопрос — повторите нажатие."


class FlakySender(RecordingSender):
    """Bot API отвечает ошибкой (429/5xx) на первых `fail_first` отправках, потом — штатно."""

    def __init__(self, *, fail_first: int) -> None:
        super().__init__()
        self._failures_left = fail_first

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise RuntimeError("Bot API: 429 Too Many Requests")
        return await super().send_message(chat_id, text, reply_markup=reply_markup)


async def _queue_down(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("Postgres недоступен")


async def _queued() -> list[dict[str, Any]]:
    """Реплики, ждущие дожима, — payload строк outbox (платформенная сессия, как воркер)."""
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_name == TelegramReplyUndelivered.event_name)
        )
        return [dict(row.payload) for row in rows]


async def _outbound_keys(tenant_id: uuid.UUID) -> list[str | None]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(
                select(Message.idempotency_key)
                .where(Message.direction == MessageDirection.OUTBOUND)
                .order_by(Message.created_at)
            )
            return list(rows)


async def test_send_reply_reports_its_outcome(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исход виден вызывающему: ушла (с message_id), в дожиме или потеряна."""
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    chat = str(GUEST_CHAT)

    with tenant_context(demo_tenant):
        sent = await send_reply(
            conversation_id, chat, "раз", sender=RecordingSender(), correlation_id="c"
        )
        queued = await send_reply(
            conversation_id, chat, "два", sender=FlakySender(fail_first=1), correlation_id="c"
        )
        monkeypatch.setattr(redelivery, "publish", _queue_down)
        lost = await send_reply(
            conversation_id, chat, "три", sender=FlakySender(fail_first=1), correlation_id="c"
        )

    assert sent == ReplyOutcome(ReplyDelivery.SENT, "m1")
    assert queued == ReplyOutcome(ReplyDelivery.QUEUED)
    assert lost == ReplyOutcome(ReplyDelivery.LOST)


async def test_keyboard_rides_the_queue(demo_tenant: uuid.UUID) -> None:
    """Клавиатура уезжает в событие и возвращается в отправке дожима."""
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    markup = consent_keyboard(None)

    with tenant_context(demo_tenant):
        await send_reply(
            conversation_id,
            str(GUEST_CHAT),
            consent_screen_text(None),
            sender=FlakySender(fail_first=1),
            correlation_id="c",
            reply_markup=markup,
        )
    (payload,) = await _queued()
    assert payload["reply_markup"] == markup

    redelivering = RecordingSender()
    with tenant_context(demo_tenant):
        await redeliver_reply(TelegramReplyUndelivered(**payload), sender=redelivering)
    assert redelivering.markups == [markup]


def test_event_queued_before_the_field_still_parses() -> None:
    """Событие, вставшее в очередь до поля `reply_markup`, разбирается как прежде."""
    event = TelegramReplyUndelivered.model_validate(
        {
            "conversation_id": str(uuid.uuid4()),
            "chat_id": str(GUEST_CHAT),
            "text": "Принял.",
            "idempotency_key": "telegram:reply:old",
            "queued_at": utc_now().isoformat(),
        }
    )
    assert event.reply_markup is None


# ---------------------------------------------------------------------------
# Экран согласия (spec 0029 §4): сквозь вебхук и цикл доставки, как в проде


@pytest.fixture
async def gate(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, FlakySender]]:
    """Вебхук без согласия гостя; первая отправка в Bot API падает."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    sender = FlakySender(fail_first=1)
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text="—")
    redelivery.register(sender=sender)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sender
    get_settings.cache_clear()


async def _guest_writes(client: AsyncClient, update_id: int) -> None:
    payload = {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": GUEST_CHAT}, "text": "здравствуйте"},
    }
    response = await client.post("/channels/telegram/webhook", json=payload, headers=AUTH)
    assert response.status_code == 200


async def test_consent_screen_survives_bot_api_failure(
    gate: tuple[AsyncClient, FlakySender], demo_tenant: uuid.UUID
) -> None:
    """Экран не ушёл — встаёт в дожим и доходит с кнопкой; ключ экрана пишется дожимом.

    До #296 гость писал второй раз в пустоту: экран повторялся только на его
    следующее сообщение.
    """
    client, sender = gate

    await _guest_writes(client, 1)
    assert sender.sent == []
    assert len(await _queued()) == 1

    assert await deliver_pending_events() == 1
    assert sender.sent == [(str(GUEST_CHAT), consent_screen_text(None))]
    assert sender.markups == [consent_keyboard(None)]
    (key,) = await _outbound_keys(demo_tenant)
    assert key is not None and key.startswith("guest:consent_prompt:")
    assert key.endswith(f":{CONSENT_VERSION}")

    # Ключ записан — следующее сообщение гостя второго экрана не даёт.
    await _guest_writes(client, 2)
    assert len(sender.sent) == 1


async def test_guest_writing_before_redelivery_gets_one_screen(
    gate: tuple[AsyncClient, FlakySender],
) -> None:
    """Гость написал снова раньше дожима: экран показан сразу, дожим второго не шлёт."""
    client, sender = gate

    await _guest_writes(client, 1)  # первая отправка упала — экран в дожиме
    await _guest_writes(client, 2)  # Bot API ожил — экран ушёл сразу
    assert len(sender.sent) == 1

    assert await deliver_pending_events() == 1
    assert len(sender.sent) == 1


# ---------------------------------------------------------------------------
# Вопрос персоналу с ForceReply (spec 0021 П-4)


@pytest.fixture
def staff_chat() -> Iterator[None]:
    """Команды идут из дефолтного чата персонала — он ведёт заявки всех служб."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TELEGRAM_STAFF_CHAT_ID", STAFF_CHAT)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def _staff_update(
    text: str, *, kind: MessageKind = MessageKind.TEXT, reply_to: ReplyTo | None = None
) -> NormalizedMessage:
    is_callback = kind is MessageKind.CALLBACK
    return NormalizedMessage(
        channel="telegram",
        chat_id=STAFF_CHAT,
        external_message_id=str(uuid.uuid4()),
        idempotency_key=f"telegram:update:{uuid.uuid4()}",
        kind=kind,
        text=text,
        reply_to=reply_to,
        callback_id=f"cb-{uuid.uuid4()}" if is_callback else None,
        actor_external_id="42",
    )


async def _staff(tenant_id: uuid.UUID, message: NormalizedMessage, sender: RecordingSender) -> None:
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", STAFF_CHAT)
        await handle_staff_message(conversation_id, message, sender=sender, correlation_id="c")


async def _request_in_progress(tenant_id: uuid.UUID) -> uuid.UUID:
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
            )
        )
    await _staff(tenant_id, _staff_update(f"/start {request.id}"), RecordingSender())
    return request.id


def _done_note_press(request_id: uuid.UUID) -> NormalizedMessage:
    data = keyboards.build_callback_data(request_id, keyboards.CallbackAction.DONE_NOTE)
    notification = ReplyTo(external_message_id="n1", text="🔔 Новая заявка #1")
    return _staff_update(data, kind=MessageKind.CALLBACK, reply_to=notification)


@pytest.mark.usefixtures("staff_chat")
async def test_note_question_survives_bot_api_failure(demo_tenant: uuid.UUID) -> None:
    """Вопрос «что не сделано?» дожимается с ForceReply, и ответ на него закрывает заявку.

    Опора ответа-реплая — `external_message_id` вопроса — пишется в момент
    дожима; ключ вопроса уезжает в очередь как есть.
    """
    request_id = await _request_in_progress(demo_tenant)
    pressing = FlakySender(fail_first=1)

    await _staff(demo_tenant, _done_note_press(request_id), pressing)

    assert pressing.sent == []
    # Часики сняты тостом ожидания: вопрос доедет дожимом.
    assert len(pressing.toasts) == 1
    assert pressing.toasts[0][1].startswith("Жду примечание")
    (payload,) = await _queued()
    assert payload["reply_markup"] == FORCE_REPLY
    assert payload["idempotency_key"].startswith(f"staff:note_prompt:{request_id}:")

    redelivering = RecordingSender()
    with tenant_context(demo_tenant):
        await redeliver_reply(TelegramReplyUndelivered(**payload), sender=redelivering)
    assert redelivering.markups == [FORCE_REPLY]
    (_chat, question) = redelivering.sent[0]

    answer = _staff_update(
        "кофе закончился", reply_to=ReplyTo(external_message_id="m1", text=question)
    )
    await _staff(demo_tenant, answer, RecordingSender())
    with tenant_context(demo_tenant):
        closed = await requests_api.get_request(request_id)
    assert closed.status is requests_api.RequestStatus.DONE
    assert closed.resolution_note == "кофе закончился"


@pytest.mark.usefixtures("staff_chat")
async def test_lost_note_question_asks_to_press_again(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Вопрос не ушёл и не встал в очередь — нажавший узнаёт об этом тостом, а не тишиной."""
    request_id = await _request_in_progress(demo_tenant)
    monkeypatch.setattr(redelivery, "publish", _queue_down)
    pressing = FlakySender(fail_first=1)

    await _staff(demo_tenant, _done_note_press(request_id), pressing)

    assert [text for _cb, text in pressing.toasts] == [RETRY_TOAST]
    with tenant_context(demo_tenant):
        still_open = await requests_api.get_request(request_id)
    assert still_open.status is requests_api.RequestStatus.IN_PROGRESS
