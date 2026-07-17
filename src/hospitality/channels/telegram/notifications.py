"""Подписчики-уведомления Telegram (Task 0017, P-6, ADR-011).

Уведомления — подписчики доменных событий, а не вызовы из `modules/requests`
(P-6). Их регистрирует composition root воркера (`hospitality/worker.py`), как
`include_router` в `app.py`; сам модуль requests о них не знает. Оба выполняются в
`tenant_context` события (его ставит доставщик outbox) и шлют через `TelegramSender`.

- `notify_staff_on_request_created` — на `request.created`: уведомить staff-чат о
  новой заявке (+ подсказать команды закрытия).
- `notify_guest_on_request_done` — на `request.status_changed` (только `→ done`):
  вернуть гостю подтверждение в его чат (адрес — по `request_origins`, ADR-011).

Идемпотентность (P-8, at-least-once ADR-005): исход фиксируется исходящим `Message`
с естественным ключом; повторная доставка события уведомление не дублирует. Сбой
ОТПРАВКИ пробрасывается — воркер ретраит с backoff (ADR-009); ключ гасит дубль на
штатной пере-доставке.
"""

from __future__ import annotations

import uuid

import structlog

from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.store import (
    ensure_conversation,
    load_conversation_external_id,
    load_request_origin_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.events import subscribe
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


def register(*, sender: TelegramSender, staff_chat_id: str) -> None:
    """Подписать уведомления Telegram на доменные события (Task 0017, P-6).

    Зовётся composition root воркера (`hospitality/worker.py`); тесты зовут с
    фейк-отправителем. Замыкания связывают отправитель и staff-chat-id с
    обработчиками — сами события их не несут.
    """

    async def on_request_created(event: requests_api.RequestCreated) -> None:
        await notify_staff_on_request_created(event, sender=sender, staff_chat_id=staff_chat_id)

    async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
        await notify_guest_on_request_done(event, sender=sender)

    subscribe(requests_api.RequestCreated, on_request_created)
    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)


GUEST_DONE_CONFIRMATION = (
    "Ваша заявка «{summary}» выполнена. Спасибо! / Your request is done, thank you!"
)


async def notify_staff_on_request_created(
    event: requests_api.RequestCreated, *, sender: TelegramSender, staff_chat_id: str
) -> None:
    """Уведомить staff-чат о новой заявке (подписчик `request.created`)."""
    if not staff_chat_id:
        logger.warning("telegram_staff_chat_not_configured", request_id=str(event.request_id))
        return

    idempotency_key = f"staff:request_created:{event.request_id}"
    if await notification_already_sent(idempotency_key):
        logger.info("staff_notification_skipped_duplicate", request_id=str(event.request_id))
        return

    conversation_id = await ensure_conversation(staff_chat_id)
    # Событие несёт только request_id/category_id/summary — комнату дочитываем из
    # заявки (как `notify_guest_on_request_done`), иначе служба не знает, куда идти
    # (S-1, #37). Контракт события не расширяем ради этого (остаётся Уровень B).
    request = await requests_api.get_request(event.request_id)
    room_line = f"🚪 Комната: {request.room_number}\n" if request.room_number else ""
    text = (
        "🔔 Новая заявка от гостя.\n"
        f"{room_line}"
        f"Категория: {await _category_name(event.category_id)}\n"
        f"Суть: {event.summary}\n\n"
        f"id: {event.request_id}\n"
        "Ход: /assign · /start · /done · /cancel + этот id."
    )
    # Отправка может упасть — тогда исключение проброшено, воркер ретраит (ключ
    # гасит дубль). Запись — только после успешной отправки (не «соврать» историей).
    sent_id = await sender.send_message(staff_chat_id, text)
    await record_outbound_message(
        conversation_id,
        text,
        _current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info("staff_notified", request_id=str(event.request_id))


async def notify_guest_on_request_done(
    event: requests_api.RequestStatusChanged, *, sender: TelegramSender
) -> None:
    """Подтвердить гостю выполнение заявки (подписчик `request.status_changed`)."""
    if event.new_status is not requests_api.RequestStatus.DONE:
        return  # гость получает подтверждение только на завершение (done)

    conversation_id = await load_request_origin_conversation(event.request_id)
    if conversation_id is None:
        # Привязки нет: заявка создана не через Telegram (например, curl-ом на API).
        logger.info("guest_notification_skipped_no_origin", request_id=str(event.request_id))
        return

    idempotency_key = f"guest:request_done:{event.request_id}"
    if await notification_already_sent(idempotency_key):
        logger.info("guest_notification_skipped_duplicate", request_id=str(event.request_id))
        return

    chat_id = await load_conversation_external_id(conversation_id)
    if chat_id is None:  # pragma: no cover — привязка ссылается на существующий диалог
        return

    request = await requests_api.get_request(event.request_id)
    text = GUEST_DONE_CONFIRMATION.format(summary=request.summary)
    sent_id = await sender.send_message(chat_id, text)
    await record_outbound_message(
        conversation_id,
        text,
        _current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    logger.info("guest_notified_done", request_id=str(event.request_id))


async def _category_name(category_id: uuid.UUID) -> str:
    """Человекочитаемое имя категории заявки для уведомления; id как фолбэк."""
    for category in await requests_api.list_categories():
        if category.id == category_id:
            return category.name
    return str(category_id)


def _current_correlation_id() -> str:
    """correlation_id события (доставщик outbox восстановил его в лог-контекст)."""
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else ""
