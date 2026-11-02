"""Ответ-ошибка Telegram Bot API без адреса запроса (#485, `shared/telegram_api.py`).

Гостевой бот проверяется рядом с ним (`channels/telegram/tests/test_client.py`),
здесь — тракт алертов и разбор ответа. Маркер токена — латиницей: JSON-логи
экранируют кириллицу.
"""

from __future__ import annotations

import traceback

import httpx
import pytest

from hospitality.shared.alerting import send_alert
from hospitality.shared.config import Settings
from hospitality.shared.telegram_api import TelegramApiError, raise_for_telegram_status

_ALERT_TOKEN = "987654321:AAHalertLeakMarker"


async def test_alert_bot_failure_raises_without_token() -> None:
    """Алерт-бот выкинут из канала (400) — сбой пробрасывается (вызывающий не
    пометит событие рассказанным), но без адреса: исключение уходит в лог и
    Sentry воркера."""

    def chat_not_found(request: httpx.Request) -> httpx.Response:
        assert _ALERT_TOKEN in str(request.url), "проба: адрес обязан нести токен"
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    settings = Settings(telegram_alert_bot_token=_ALERT_TOKEN, telegram_alert_chat_id="-100")

    with pytest.raises(TelegramApiError) as raised:
        await send_alert(settings, "алерт", transport=httpx.MockTransport(chat_not_found))

    assert str(raised.value) == "Bot API sendMessage: HTTP 400: Bad Request: chat not found"
    assert "AAHalertLeakMarker" not in "".join(traceback.format_exception(raised.value))


def test_non_json_error_body_falls_back_to_status_phrase() -> None:
    """502 от прокси с HTML-телом: описания Telegram нет — диагноз из фразы статуса,
    а чужое тело в исключение не попадает."""
    response = httpx.Response(502, text="<html>bad gateway page</html>")

    with pytest.raises(TelegramApiError) as raised:
        raise_for_telegram_status(response, "sendMessage")

    assert str(raised.value) == "Bot API sendMessage: HTTP 502: Bad Gateway"


def test_success_response_raises_nothing() -> None:
    raise_for_telegram_status(httpx.Response(200, json={"ok": True}), "sendMessage")
