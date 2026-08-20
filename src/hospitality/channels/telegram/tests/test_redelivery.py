"""Ответ гостю не теряется при сбое Bot API (issue #209).

Каждая ветка поведения — свой тест: первая попытка удалась; упала и встала в
очередь; очередь дожала; дожим не удался (ретрай воркера); реплика протухла;
очередь не приняла реплику. Сквозной сценарий — на каноне walking-skeleton:
ASGI (`create_app`) + та же доставка outbox, что в проде (`deliver_pending_events`),
но инлайн.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.app import create_app
from hospitality.channels.common.models import Message, MessageDirection
from hospitality.channels.telegram import redelivery
from hospitality.channels.telegram.outbound import send_reply
from hospitality.channels.telegram.redelivery import (
    REPLY_MAX_AGE_SECONDS,
    GuestReplyUndelivered,
    queue_undelivered_reply,
    redeliver_guest_reply,
)
from hospitality.channels.telegram.router import get_orchestrator_provider, get_telegram_sender
from hospitality.channels.telegram.tests.conftest import RecordingSender, grant_consent
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.events import OutboxEvent, deliver_pending_events
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context

SECRET = "test-webhook-secret"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
GUEST_CHAT = 555
CORRELATION = "corr-209"
REPLY = "Принял, полотенца несут."


class FailingSender(RecordingSender):
    """Bot API отвечает ошибкой (429/5xx) — то, что раньше съедалось WARNING'ом.

    Наследник запоминающего фейка: попытки видны в `attempts`, а `sent` остаётся
    пустым — «отправленным» ничего не считается.
    """

    def __init__(self, *, fail_first: int = 10_000) -> None:
        super().__init__()
        self.attempts: list[tuple[str, str]] = []
        self._fail_first = fail_first

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        self.attempts.append((chat_id, text))
        if len(self.attempts) <= self._fail_first:
            raise RuntimeError("Bot API: 429 Too Many Requests")
        return await super().send_message(chat_id, text, reply_markup=reply_markup)


def _event(conversation_id: uuid.UUID, *, age_seconds: float = 0.0) -> GuestReplyUndelivered:
    """Реплика, ждущая дожима: `age_seconds` — сколько она уже пролежала."""
    return GuestReplyUndelivered(
        conversation_id=conversation_id,
        chat_id=str(GUEST_CHAT),
        text=REPLY,
        idempotency_key="guest:reply:test",
        queued_at=utc_now() - timedelta(seconds=age_seconds),
    )


async def _outbox_replies() -> list[dict[str, Any]]:
    """Реплики, ждущие дожима, — строками outbox (платформенная сессия, как воркер)."""
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_name == GuestReplyUndelivered.event_name)
        )
        return [dict(row.payload) for row in rows]


async def _outbound_texts(tenant_id: uuid.UUID) -> list[str]:
    """Исходящие реплики в истории диалога — то, что гость действительно видел."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(
                select(Message.text).where(Message.direction == MessageDirection.OUTBOUND)
            )
            return [row for row in rows if row is not None]


def _log_events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]


async def test_successful_send_is_recorded_and_not_queued(demo_tenant: uuid.UUID) -> None:
    """Штатный путь не изменился: реплика ушла, легла в историю, очереди нет."""
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await send_reply(
            conversation_id, str(GUEST_CHAT), REPLY, sender=sender, correlation_id=CORRELATION
        )

    assert sender.sent == [(str(GUEST_CHAT), REPLY)]
    assert await _outbound_texts(demo_tenant) == [REPLY]
    assert await _outbox_replies() == []


async def test_failed_send_is_queued_and_stays_out_of_history(demo_tenant: uuid.UUID) -> None:
    """Сбой Bot API ставит реплику в очередь и НЕ пишет её в историю.

    Две половины одного инварианта: реплика не потеряна (строка outbox есть) и
    история не соврала (гость её не видел — значит, `Message` нет).
    """
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = FailingSender()

    with tenant_context(demo_tenant):
        await send_reply(
            conversation_id, str(GUEST_CHAT), REPLY, sender=sender, correlation_id=CORRELATION
        )

    assert sender.attempts == [(str(GUEST_CHAT), REPLY)]
    assert await _outbound_texts(demo_tenant) == []
    queued = await _outbox_replies()
    assert len(queued) == 1
    assert queued[0]["chat_id"] == str(GUEST_CHAT)
    assert queued[0]["text"] == REPLY
    assert queued[0]["conversation_id"] == str(conversation_id)
    # Обычная реплика хода естественного ключа не имеет — синтезируется свой,
    # иначе повторная доставка события (at-least-once) отправила бы её дважды.
    assert queued[0]["idempotency_key"].startswith("guest:reply:")


async def test_two_failed_replies_keep_separate_keys(demo_tenant: uuid.UUID) -> None:
    """Синтетический ключ — на реплику, а не на диалог: обе доходят.

    Ключ, общий на диалог, проглотил бы вторую реплику как дубль первой — и
    сделал бы это молча, гарду идемпотентности такое неотличимо от повторной
    доставки события. Гость при этом снова остался бы без ответа (issue #209),
    только на шаг позже.
    """
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = FailingSender()

    with tenant_context(demo_tenant):
        await send_reply(
            conversation_id, str(GUEST_CHAT), "Принял.", sender=sender, correlation_id=CORRELATION
        )
        await send_reply(
            conversation_id,
            str(GUEST_CHAT),
            "И ещё принял.",
            sender=sender,
            correlation_id=CORRELATION,
        )

    queued = await _outbox_replies()
    assert len({row["idempotency_key"] for row in queued}) == 2

    redelivering = RecordingSender()
    with tenant_context(demo_tenant):
        for row in queued:
            await redeliver_guest_reply(GuestReplyUndelivered(**row), sender=redelivering)
    assert sorted(text for _, text in redelivering.sent) == ["И ещё принял.", "Принял."]


async def test_natural_key_survives_the_queue(demo_tenant: uuid.UUID) -> None:
    """Реплика с естественным ключом уносит его в очередь (spec 0029 §4).

    Приветствие после согласия — «один раз на (диалог, версия)»; дожим обязан
    держать ТОТ ЖЕ ключ, иначе обошёл бы собственную идемпотентность реплики.
    """
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    key = f"guest:consent_granted:{conversation_id}:v1"

    with tenant_context(demo_tenant):
        await send_reply(
            conversation_id,
            str(GUEST_CHAT),
            "Здравствуйте!",
            sender=FailingSender(),
            correlation_id=CORRELATION,
            idempotency_key=key,
        )

    assert [row["idempotency_key"] for row in await _outbox_replies()] == [key]


async def test_queue_failure_is_loud_and_does_not_break_the_webhook(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """БД не приняла реплику в очередь — это ERROR ERR-TELEGRAM-006, а не тишина.

    Единственный оставшийся путь окончательной потери. Наружу он не бросает:
    500 в ответ Telegram'у ничего не чинит (повтор апдейта съест дедупликация),
    зато откатил бы уже сделанную работу хода.
    """
    configure_logging()
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Postgres недоступен")

    monkeypatch.setattr(redelivery, "publish", boom)

    with tenant_context(demo_tenant):
        await send_reply(
            conversation_id,
            str(GUEST_CHAT),
            REPLY,
            sender=FailingSender(),
            correlation_id=CORRELATION,
        )

    lost = [event for event in _log_events(capsys) if event.get("event") == "telegram_reply_lost"]
    assert len(lost) == 1
    assert lost[0]["level"] == "error"
    assert lost[0]["error_code"] == "ERR-TELEGRAM-006"
    assert lost[0]["reason"] == "queue_failed"
    assert await _outbox_replies() == []


async def test_redelivery_sends_and_records(demo_tenant: uuid.UUID) -> None:
    """Дожим: реплика уходит гостю и только тогда попадает в историю."""
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await redeliver_guest_reply(_event(conversation_id), sender=sender)

    assert sender.sent == [(str(GUEST_CHAT), REPLY)]
    assert await _outbound_texts(demo_tenant) == [REPLY]


async def test_redelivery_is_idempotent(demo_tenant: uuid.UUID) -> None:
    """Повторная доставка события (at-least-once, ADR-005) не шлёт второй раз (P-8)."""
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = RecordingSender()
    event = _event(conversation_id)

    with tenant_context(demo_tenant):
        await redeliver_guest_reply(event, sender=sender)
        await redeliver_guest_reply(event, sender=sender)

    assert len(sender.sent) == 1
    assert len(await _outbound_texts(demo_tenant)) == 1


async def test_redelivery_failure_propagates_to_the_worker(demo_tenant: uuid.UUID) -> None:
    """Сбой дожима ПРОБРАСЫВАЕТСЯ: это и есть ретрай (backoff ADR-009).

    Проглоти подписчик исключение — воркер счёл бы событие доставленным, и мы
    вернулись бы ровно к issue #209, только этажом ниже.
    """
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)

    with tenant_context(demo_tenant), pytest.raises(RuntimeError):
        await redeliver_guest_reply(_event(conversation_id), sender=FailingSender())

    assert await _outbound_texts(demo_tenant) == []


async def test_stale_reply_is_dropped_loudly(
    demo_tenant: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    """Протухшая реплика гостю НЕ отправляется — но и не пропадает молча.

    Ответ на ход, случившийся полчаса назад, дезориентирует сильнее молчания
    (диалог ушёл вперёд). Поэтому вместо отправки — ERROR ERR-TELEGRAM-006.
    """
    configure_logging()
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await redeliver_guest_reply(
            _event(conversation_id, age_seconds=REPLY_MAX_AGE_SECONDS + 1), sender=sender
        )

    assert sender.sent == []
    assert await _outbound_texts(demo_tenant) == []
    lost = [item for item in _log_events(capsys) if item.get("event") == "telegram_reply_lost"]
    assert len(lost) == 1
    assert lost[0]["level"] == "error"
    assert lost[0]["error_code"] == "ERR-TELEGRAM-006"
    assert lost[0]["reason"] == "too_stale"


async def test_reply_inside_the_deadline_is_still_sent(demo_tenant: uuid.UUID) -> None:
    """Реплика внутри срока годности отправляется — дожим не выродился в отказ.

    Щит от противоположной ошибки: сравнение, перевёрнутое или сбитое до нуля,
    погасило бы весь дожим, а тест выше остался бы зелёным.
    """
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    sender = RecordingSender()

    with tenant_context(demo_tenant):
        await redeliver_guest_reply(
            _event(conversation_id, age_seconds=REPLY_MAX_AGE_SECONDS - 30), sender=sender
        )

    assert len(sender.sent) == 1


@pytest.fixture
async def webhook_stand(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, FailingSender, FastAPI]]:
    """Стенд сквозного сценария: вебхук с падающим Bot API, consent пройден."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    sender = FailingSender(fail_first=1)
    app = create_app()
    app.dependency_overrides[get_telegram_sender] = lambda: sender
    app.dependency_overrides[get_orchestrator_provider] = lambda: MockLlmProvider(text=REPLY)
    await grant_consent(demo_tenant, GUEST_CHAT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sender, app
    get_settings.cache_clear()


async def test_webhook_reply_survives_bot_api_failure(
    webhook_stand: tuple[AsyncClient, FailingSender, FastAPI], demo_tenant: uuid.UUID
) -> None:
    """Сквозной DoD issue #209: 429 на ответе гостю — и гость всё равно его получает.

    Первая отправка падает, вебхук отвечает 200 (иначе Telegram ретраит апдейт, а
    его съедает дедупликация), реплика встаёт в outbox. Тот же цикл доставки, что
    в проде, дожимает её вторым вызовом Bot API.
    """
    client, sender, _ = webhook_stand
    redelivery.register(sender=sender)

    response = await client.post(
        "/channels/telegram/webhook",
        json={
            "update_id": 1,
            "message": {"message_id": 1, "chat": {"id": GUEST_CHAT}, "text": "нужны полотенца"},
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert sender.sent == []  # первая попытка провалилась
    assert len(await _outbox_replies()) == 1

    assert await deliver_pending_events() == 1
    assert sender.sent == [(str(GUEST_CHAT), REPLY)]
    assert await _outbound_texts(demo_tenant) == [REPLY]


async def test_queued_reply_belongs_to_its_tenant(demo_tenant: uuid.UUID) -> None:
    """Постановка в очередь идёт в контексте тенанта (P-4): событие чужим не станет."""
    conversation_id = await grant_consent(demo_tenant, GUEST_CHAT)
    with tenant_context(demo_tenant):
        await queue_undelivered_reply(conversation_id, str(GUEST_CHAT), REPLY, idempotency_key=None)
    async with platform_session_scope() as session:
        tenant_ids = list(
            await session.scalars(
                select(OutboxEvent.tenant_id).where(
                    OutboxEvent.event_name == GuestReplyUndelivered.event_name
                )
            )
        )
    assert tenant_ids == [demo_tenant]
