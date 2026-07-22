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


class TelegramUser(BaseModel):
    """Автор сообщения/нажатия (нужен только id — для логов «кто сделал»)."""

    model_config = ConfigDict(extra="ignore")

    id: int


class TelegramMessage(BaseModel):
    """Сообщение Telegram. Рекурсивно ссылается на себя через `reply_to_message`."""

    model_config = ConfigDict(extra="ignore")

    message_id: int
    chat: TelegramChat
    # Автор («from» — ключевое слово Python, поэтому alias). Для структурных логов.
    from_user: TelegramUser | None = Field(default=None, alias="from")
    # text отсутствует у не-текстовых сообщений (фото, стикер, голос, …).
    text: str | None = None
    # Полный объект сообщения, на которое ответил гость (reply): Telegram даёт его
    # целиком — reply_to заполняется без дозапросов (см. channels.base.ReplyTo).
    reply_to_message: TelegramMessage | None = None


class TelegramCallbackQuery(BaseModel):
    """Нажатие inline-кнопки (spec 0021 П-2). `id` нужен для answerCallbackQuery
    (иначе у нажавшего крутится «часики»), `message` — сообщение с кнопками."""

    model_config = ConfigDict(extra="ignore")

    id: str
    from_user: TelegramUser | None = Field(default=None, alias="from")
    data: str | None = None
    message: TelegramMessage | None = None


class TelegramUpdate(BaseModel):
    """Одно обновление вебхука. `update_id` — ключ идемпотентности доставки (P-8)."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    # None для типов обновлений, которые канал не обрабатывает (edited_message,
    # channel_post, …) — вебхук отвечает 200 без побочных эффектов.
    message: TelegramMessage | None = None
    # Нажатие inline-кнопки в staff-чате (spec 0021 П-2); None у обычных сообщений.
    callback_query: TelegramCallbackQuery | None = None


class TelegramWebhookAck(BaseModel):
    """Ответ вебхука. Telegram нужен любой 2xx; тело — для читаемости логов/curl."""

    ok: bool = Field(default=True)
