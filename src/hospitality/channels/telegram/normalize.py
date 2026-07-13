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

    None означает «валидное обновление, но не сообщение, которое канал ведёт в
    Phase 0» (edited_message, callback_query и т.п.) — вебхук ответит 200 без
    побочных эффектов.
    """
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
    )


def _normalize_reply_to(replied: TelegramMessage | None) -> ReplyTo | None:
    """Reply-контекст из полного объекта Telegram (текст доступен сразу, §DISCUSSION_LOG)."""
    if replied is None:
        return None
    return ReplyTo(external_message_id=str(replied.message_id), text=replied.text)
