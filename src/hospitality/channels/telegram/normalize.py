"""Нормализация payload Telegram → `channels.base.NormalizedMessage` (Task 0016, P-7).

Чистая функция без побочных эффектов: разбирает `TelegramUpdate` в общий контракт
канала. Классификация Phase 0 проста — есть текст → `TEXT`, иначе → `UNSUPPORTED`.
Персистентность и отправка ответа — не здесь (см. `store.py`, `service.py`).
"""

from __future__ import annotations

from hospitality.channels.base import MessageKind, NormalizedMessage, ReplyTo
from hospitality.channels.telegram.schemas import TelegramMessage, TelegramUpdate

CHANNEL = "telegram"

# Namespace ключа идемпотентности: update_id уникален у Telegram, но общий
# unique-constraint Message живёт на (tenant_id, idempotency_key) без канала —
# префикс исключает коллизию с ключами будущих каналов (WhatsApp/Email).
_IDEMPOTENCY_PREFIX = "telegram:update:"


def normalize_update(update: TelegramUpdate) -> NormalizedMessage | None:
    """Привести обновление к нормализованному сообщению; None — обрабатывать нечего.

    None означает «валидное обновление, но не то, что канал ведёт»
    (edited_message, channel_post и т.п.) — вебхук ответит 200 без побочных
    эффектов. Нажатие inline-кнопки (`callback_query`) — полноценное входящее
    вида CALLBACK (spec 0021 П-2).
    """
    if update.callback_query is not None:
        return _normalize_callback(update)

    message = update.message
    if message is None:
        return None

    text = message.text
    kind = MessageKind.TEXT if text is not None else MessageKind.UNSUPPORTED

    return NormalizedMessage(
        channel=CHANNEL,
        chat_id=str(message.chat.id),
        external_message_id=str(message.message_id),
        idempotency_key=f"{_IDEMPOTENCY_PREFIX}{update.update_id}",
        kind=kind,
        text=text,
        reply_to=_normalize_reply_to(message.reply_to_message),
        actor_external_id=str(message.from_user.id) if message.from_user else None,
    )


def _normalize_callback(update: TelegramUpdate) -> NormalizedMessage | None:
    """Нажатие кнопки → CALLBACK: `text` — callback_data, `reply_to` — сообщение
    с кнопками (кнопка ≈ ответ на своё сообщение), `callback_id` — для тоста.

    Без `message` (сообщение слишком старое у Telegram) или без `data` кнопку
    не к чему привязать — no-op 200.
    """
    callback = update.callback_query
    assert callback is not None  # вызывается только из ветки выше
    message = callback.message
    if message is None or callback.data is None:
        return None
    return NormalizedMessage(
        channel=CHANNEL,
        chat_id=str(message.chat.id),
        # У нажатия нет своего message_id — берём уникальный id callback-запроса
        # с префиксом, чтобы не коллизировать с настоящими message_id.
        external_message_id=f"callback:{callback.id}",
        idempotency_key=f"{_IDEMPOTENCY_PREFIX}{update.update_id}",
        kind=MessageKind.CALLBACK,
        text=callback.data,
        reply_to=ReplyTo(external_message_id=str(message.message_id), text=message.text),
        callback_id=callback.id,
        actor_external_id=str(callback.from_user.id) if callback.from_user else None,
    )


def _normalize_reply_to(replied: TelegramMessage | None) -> ReplyTo | None:
    """Reply-контекст из полного объекта Telegram (текст доступен сразу, §DISCUSSION_LOG)."""
    if replied is None:
        return None
    return ReplyTo(external_message_id=str(replied.message_id), text=replied.text)
