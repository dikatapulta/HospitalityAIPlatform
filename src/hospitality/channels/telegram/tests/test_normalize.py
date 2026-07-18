"""Нормализация payload Telegram → NormalizedMessage (Task 0016, P-7).

Чистые тесты без БД: классификация текст/не-текст, ключ идемпотентности,
reply-контекст, обработка не-сообщений.
"""

from __future__ import annotations

from hospitality.channels.base import MessageKind
from hospitality.channels.telegram.normalize import normalize_update
from hospitality.channels.telegram.schemas import (
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
)


def _text_update(update_id: int = 42, text: str = "уберите номер 305") -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=TelegramMessage(message_id=7, chat=TelegramChat(id=555), text=text),
    )


def test_text_message_normalized_to_text_kind() -> None:
    normalized = normalize_update(_text_update())
    assert normalized is not None
    assert normalized.channel == "telegram"
    assert normalized.chat_id == "555"
    assert normalized.external_message_id == "7"
    assert normalized.kind is MessageKind.TEXT
    assert normalized.text == "уберите номер 305"
    # Ключ идемпотентности namespace'нут — не коллизирует с другими каналами.
    assert normalized.idempotency_key == "telegram:update:42"
    assert normalized.reply_to is None


def test_non_text_message_normalized_to_unsupported() -> None:
    # Сообщение без текста (фото/стикер/голос): text=None → kind=UNSUPPORTED.
    update = TelegramUpdate(
        update_id=43,
        message=TelegramMessage(message_id=8, chat=TelegramChat(id=555), text=None),
    )
    normalized = normalize_update(update)
    assert normalized is not None
    assert normalized.kind is MessageKind.UNSUPPORTED
    assert normalized.text is None


def test_reply_to_is_extracted_from_full_object() -> None:
    # Telegram присылает полный объект reply — текст доступен сразу (DISCUSSION_LOG).
    update = TelegramUpdate(
        update_id=44,
        message=TelegramMessage(
            message_id=9,
            chat=TelegramChat(id=555),
            text="да",
            reply_to_message=TelegramMessage(
                message_id=7, chat=TelegramChat(id=555), text="Оформить уборку номера 305?"
            ),
        ),
    )
    normalized = normalize_update(update)
    assert normalized is not None
    assert normalized.reply_to is not None
    assert normalized.reply_to.external_message_id == "7"
    assert normalized.reply_to.text == "Оформить уборку номера 305?"


def test_non_message_update_returns_none() -> None:
    # Обновление без message (edited_message, callback_query, …) — обрабатывать нечего.
    assert normalize_update(TelegramUpdate(update_id=45, message=None)) is None


def test_callback_query_normalized_to_callback_kind() -> None:
    """Нажатие inline-кнопки → CALLBACK (spec 0021 П-2): text — callback_data,
    reply_to — сообщение с кнопками, callback_id — для тоста, actor — кто нажал."""
    update = TelegramUpdate.model_validate(
        {
            "update_id": 77,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 42},
                "data": "req:00000000-0000-0000-0000-000000000001:start",
                "message": {
                    "message_id": 10,
                    "chat": {"id": 999},
                    "text": "🔔 Новая заявка #1",
                },
            },
        }
    )
    normalized = normalize_update(update)
    assert normalized is not None
    assert normalized.kind is MessageKind.CALLBACK
    assert normalized.chat_id == "999"
    assert normalized.text == "req:00000000-0000-0000-0000-000000000001:start"
    assert normalized.callback_id == "cb-1"
    assert normalized.actor_external_id == "42"
    assert normalized.reply_to is not None
    assert normalized.reply_to.external_message_id == "10"
    assert normalized.idempotency_key == "telegram:update:77"


def test_callback_without_message_is_noop() -> None:
    """Callback без message (Telegram отдал слишком старое сообщение) → no-op 200."""
    update = TelegramUpdate.model_validate(
        {"update_id": 78, "callback_query": {"id": "cb-2", "data": "req:x:start"}}
    )
    assert normalize_update(update) is None


def test_text_message_carries_actor() -> None:
    """Автор текстового сообщения попадает в actor_external_id (логи «кто скомандовал»)."""
    update = TelegramUpdate.model_validate(
        {
            "update_id": 79,
            "message": {
                "message_id": 11,
                "chat": {"id": 999},
                "from": {"id": 43},
                "text": "/done 1",
            },
        }
    )
    normalized = normalize_update(update)
    assert normalized is not None
    assert normalized.actor_external_id == "43"
