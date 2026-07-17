"""Payload Telegram Bot API и ответ вебхука (Task 0016, R-6, P-7).

Разбираем только то подмножество `Update`, что нужно Phase 0 (текст личного чата +
reply-контекст). `extra="ignore"`: Telegram шлёт десятки типов обновлений и полей;
незнакомые поля не должны ронять вебхук — неизвестный тип обновления даёт
`message is None` и обрабатывается как no-op. Это приватная деталь адаптера:
наружу канал отдаёт `channels.base.NormalizedMessage`, а не эти модели.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TelegramChat(BaseModel):
    """Чат, из которого пришло сообщение (нужен только id)."""

    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramMessage(BaseModel):
    """Сообщение Telegram. Рекурсивно ссылается на себя через `reply_to_message`."""

    model_config = ConfigDict(extra="ignore")

    message_id: int
    chat: TelegramChat
    # text отсутствует у не-текстовых сообщений (фото, стикер, голос, …).
    text: str | None = None
    # Полный объект сообщения, на которое ответил гость (reply): Telegram даёт его
    # целиком — reply_to заполняется без дозапросов (см. channels.base.ReplyTo).
    reply_to_message: TelegramMessage | None = None


class TelegramUpdate(BaseModel):
    """Одно обновление вебхука. `update_id` — ключ идемпотентности доставки (P-8)."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    # None для типов обновлений, которые Phase 0 не обрабатывает (edited_message,
    # callback_query, channel_post, …) — вебхук отвечает 200 без побочных эффектов.
    message: TelegramMessage | None = None


class TelegramWebhookAck(BaseModel):
    """Ответ вебхука. Telegram нужен любой 2xx; тело — для читаемости логов/curl."""

    ok: bool = Field(default=True)
