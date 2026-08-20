"""Отправка ответов в Telegram (Task 0016, §8 «те же требования устойчивости»).

`TelegramSender` — узкий порт отправки, чтобы вебхук не зависел от HTTP-клиента:
боевая реализация ходит в Bot API, тесты подставляют запоминающий фейк (каналы —
не порты ядра, обязательного Fake-адаптера нет; §8). Порт только шлёт: сбой сети
он пробрасывает, а что с ним делать — решает вызывающая сторона. У неё два ответа:
реплика гостю уходит в дожим через outbox (`redelivery.py`, issue #209), а тост,
клавиатура и прочие удобства остаются best-effort — вебхук ни то, ни другое не
роняет (иначе Telegram ретраил бы уже сохранённое сообщение).
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from hospitality.shared.config import Settings
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Таймаут одного вызова Bot API: ответ гостю не должен подвешивать обработку
# вебхука. Ретраев внутри порта нет намеренно — ждать их значило бы держать
# вебхук; повтор отправки живёт этажом выше, в дожиме через outbox (issue #209).
_SEND_TIMEOUT_SECONDS = 10.0


class TelegramSender(Protocol):
    """Порт отправки в чат Telegram (+кнопки и тосты, spec 0021 П-2)."""

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        """Отправить текст (опц. с клавиатурой); вернуть message_id (или None)."""
        ...

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        """Короткий «тост» нажавшему кнопку (иначе у него крутится ожидание)."""
        ...

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        """Заменить клавиатуру под сообщением; None — убрать кнопки."""
        ...


class HttpxTelegramSender:
    """Боевая отправка через Telegram Bot API (`sendMessage`).

    `transport` — точка подмены для тестов (httpx.MockTransport): проверить форму
    запроса к Bot API, не выходя в сеть. Прод передаёт None — httpx берёт
    обычный сетевой транспорт.
    """

    def __init__(
        self,
        bot_token: str,
        api_base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._api_base_url = api_base_url.rstrip("/")
        self._transport = transport

    async def send_message(
        self, chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> str | None:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        payload = await self._post("sendMessage", body)
        result = payload.get("result") if isinstance(payload, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return str(message_id) if message_id is not None else None

    async def answer_callback_query(self, callback_id: str, text: str) -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str, reply_markup: dict[str, Any] | None
    ) -> None:
        body: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        await self._post("editMessageReplyMarkup", body)

    async def _post(self, method: str, body: dict[str, Any]) -> object:
        """Один вызов Bot API; ошибки HTTP пробрасываются — что с ними делать,
        решает вызывающая сторона, как и у send_message."""
        url = f"{self._api_base_url}/bot{self._bot_token}/{method}"
        async with httpx.AsyncClient(
            timeout=_SEND_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            return response.json()


def build_telegram_sender(settings: Settings) -> TelegramSender:
    """Собрать боевого отправителя из настроек окружения (composition, P-12).

    Пустой `TELEGRAM_BOT_TOKEN` не мешает собрать отправитель: реальный вызов при
    пустом токене упадёт понятной ошибкой Bot API — реплика встанет в дожим
    и там же дойдёт до человека кодом ERR-TELEGRAM-006 (issue #209), остальное
    останется строкой в логе. В тестах отправитель подменяется фейком.
    """
    return HttpxTelegramSender(
        bot_token=settings.telegram_bot_token,
        api_base_url=settings.telegram_api_base_url,
    )
