"""Боевой отправитель Telegram: форма запроса к Bot API (Task 0016).

Без сети: httpx.MockTransport перехватывает вызов и проверяет URL, метод и тело
`sendMessage`, а также разбор `message_id` из ответа. Каналы — не порты ядра (§8),
Fake-контракта нет; это узкий тест транспортной формы.
"""

from __future__ import annotations

import json
import traceback

import httpx
import pytest

from hospitality.channels.telegram.client import HttpxTelegramSender
from hospitality.shared.telegram_api import TelegramApiError


async def test_send_message_posts_to_bot_api_and_parses_message_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 555}})

    sender = HttpxTelegramSender(
        bot_token="123:ABC",
        api_base_url="https://api.telegram.org",
        transport=httpx.MockTransport(handler),
    )

    sent_id = await sender.send_message("424242", "Готово")

    assert sent_id == "555"
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert seen["body"] == {"chat_id": "424242", "text": "Готово"}


async def test_send_message_returns_none_when_result_has_no_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    sender = HttpxTelegramSender(
        bot_token="123:ABC",
        api_base_url="https://api.telegram.org/",  # хвостовой слэш нормализуется
        transport=httpx.MockTransport(handler),
    )

    assert await sender.send_message("1", "hi") is None


async def test_bot_api_error_carries_diagnosis_but_not_the_token() -> None:
    """Ответ-ошибка Bot API — `TelegramApiError`: метод, код и описание Telegram,
    а адреса с токеном нет ни в тексте, ни в цепочке трейсбека (#485)."""

    def blocked_by_user(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
        )

    sender = HttpxTelegramSender(
        bot_token="123456:AAHleakMarker",
        api_base_url="https://api.telegram.org",
        transport=httpx.MockTransport(blocked_by_user),
    )

    with pytest.raises(TelegramApiError) as raised:
        await sender.answer_callback_query("cb-1", "Принято")

    error = raised.value
    assert (error.method, error.status_code) == ("answerCallbackQuery", 403)
    assert str(error) == (
        "Bot API answerCallbackQuery: HTTP 403: Forbidden: bot was blocked by the user"
    )
    assert "AAHleakMarker" not in "".join(traceback.format_exception(error))
