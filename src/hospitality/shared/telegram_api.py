"""Ответ-ошибка Telegram Bot API без адреса запроса (#485).

У Bot API токен бота — часть пути (`/bot<токен>/sendMessage`), а
``httpx.HTTPStatusError`` кладёт полный адрес запроса в свой текст. Текст
исключения расходится дальше сам: строки логов ``error=str(error)``, ERROR-событие
Sentry воркера (``event_delivery_failed`` с трейсбеком), ``outbox_events.last_error``
(БД и все бэкапы) и текст алерта dead-letter в чате команды (аудит 26.10, #485).

Поэтому отправитель Bot API не зовёт ``raise_for_status()``, а превращает ответ-ошибку
в ``TelegramApiError`` сам: метод, код HTTP и описание Telegram — это и есть диагноз
(«Forbidden: bot was blocked by the user»), а адреса в исключении нет вовсе. Ничего
не поднимается внутри ``except``, поэтому и цепочки исключений (``__context__``) с
адресом за ним нет. Отправителей два: гостевой бот (``channels/telegram/client.py``)
и тракт алертов (``shared/alerting.py``); третий, кто пойдёт в Bot API, зовёт тот же
``raise_for_telegram_status``.

Сетевые сбои (``httpx.ConnectError``, таймауты) пробрасываются как есть: их текст —
сообщение ОС или «timed out», адреса в нём нет.
"""

from __future__ import annotations

import httpx

# Сколько символов описания Telegram держит исключение: ответ Bot API короткий,
# а за предел уходит только чужое тело (HTML прокси на 502), которое здесь не нужно.
_DESCRIPTION_LIMIT = 200


class TelegramApiError(Exception):
    """Bot API ответил ошибкой; текст — метод, код и описание, без адреса и токена."""

    def __init__(self, *, method: str, status_code: int, description: str) -> None:
        super().__init__(f"Bot API {method}: HTTP {status_code}: {description}")
        self.method = method
        self.status_code = status_code
        self.description = description


def raise_for_telegram_status(response: httpx.Response, method: str) -> None:
    """Ответ-ошибка Bot API → ``TelegramApiError``; успешный ответ — ничего."""
    if response.is_success:
        return
    raise TelegramApiError(
        method=method, status_code=response.status_code, description=_description(response)
    )


def _description(response: httpx.Response) -> str:
    """Поле ``description`` ответа Bot API; не JSON или поля нет — фраза статуса HTTP."""
    try:
        payload = response.json()
    except ValueError:  # не JSON (HTML прокси) или битая кодировка
        payload = None
    description = payload.get("description") if isinstance(payload, dict) else None
    if not isinstance(description, str) or not description:
        description = response.reason_phrase or "нет описания"
    return description[:_DESCRIPTION_LIMIT]
