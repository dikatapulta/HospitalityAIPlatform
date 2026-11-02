"""Доставка алертов алертера в Telegram: «доложено» — только после доставки (issue #288).

До #288 машина состояний (``AlertMonitor``) переключала флаг «доложено», решая, что
пора сказать, а отправка шла следом и на сбое лишь писала ERROR. Алерт состояния
шлётся один раз на вход в проблему, поэтому одна неудачная минута у Telegram Bot API
или сети сервера отменяла его насовсем, а не откладывала: вернуть его мог только
перезапуск контейнера. Дороже всех это стоило ERR-OPS-008 «свежего бэкапа нет» —
такая тишина не проявляется ничем до дня аварии.

Теперь исход отправки — ``SendOutcome``, и по нему ``AlertMonitor.deliver`` решает
судьбу состояния линии:

- ``DELIVERED`` — сообщение в канале, новое состояние линии остаётся.
- ``RETRY`` — сбой временный: сеть, таймаут, 429 или 5xx Bot API. Состояние линии
  откатывается, и следующий опрос решает заново, по своим числам: проблема ещё
  есть — алерт уйдёт, уже прошла — не уйдёт ни алерт, ни ✅.
- ``REJECTED`` — Bot API отказал 4xx (кроме 429): отозван токен, бот удалён из чата,
  группа стала супергруппой. Повтор того же запроса не поможет, а каждую минуту
  слал бы ERROR в Sentry; состояние остаётся, алерт потерян, ERROR — в Sentry (#229).
  Такой канал чинят правкой ``.env`` и перезапуском алертера, а перезапуск и так
  начинает состояние с чистого листа — все активные проблемы доложатся заново.

ERROR ``alert_send_failed`` пишется на вход в серию временных сбоев и на каждый отказ
4xx; повторы внутри серии — WARNING ``alert_send_retry``, конец серии — INFO
``alert_send_recovered``. Иначе часовой сбой Telegram дал бы шестьдесят событий
Sentry об одной причине.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import httpx

from hospitality.shared.alerting import alert_request
from hospitality.shared.config import Settings
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Сколько символов ответа Bot API класть в лог: описание ошибки короткое, а за
# предел уходит только чужое тело (HTML прокси на 502).
_DETAIL_LIMIT = 300


class SendOutcome(Enum):
    """Исход одной отправки — по нему машина решает, оставить ли новое состояние."""

    DELIVERED = "delivered"
    RETRY = "retry"
    REJECTED = "rejected"


@dataclass
class TelegramAlertChannel:
    """Канал команды в Telegram: отправка алерта и учёт серии временных сбоев."""

    client: httpx.Client
    settings: Settings
    failing: bool = False

    def send(self, text: str) -> SendOutcome:
        """Отправить алерт; сбой не бросает — он становится исходом (см. модуль)."""
        url, payload = alert_request(self.settings, text)
        # Без exc_info: в тексте исключения httpx — полный адрес запроса, а в нём
        # токен бота (`/bot<ТОКЕН>/sendMessage`); ERROR-лог уходит в Sentry (#229).
        try:
            response = self.client.post(url, json=payload)
        except httpx.HTTPError as error:
            return self._retry_later(text, error=type(error).__name__)
        if response.is_success:
            if self.failing:
                logger.info("alert_send_recovered")
            self.failing = False
            logger.info("alert_sent", text=text)
            return SendOutcome.DELIVERED
        # Описание ошибки от Telegram («chat not found», «bot was kicked») —
        # без токена; это и есть диагноз.
        detail = response.text[:_DETAIL_LIMIT]
        if response.status_code == 429 or response.status_code >= 500:
            return self._retry_later(text, status_code=response.status_code, detail=detail)
        self.failing = False  # Telegram ответил — сеть жива, отказ про сам запрос
        logger.error(
            "alert_send_failed",
            text=text,
            status_code=response.status_code,
            detail=detail,
            retry=False,
        )
        return SendOutcome.REJECTED

    def _retry_later(self, text: str, **diagnosis: str | int) -> SendOutcome:
        if self.failing:
            logger.warning("alert_send_retry", text=text, **diagnosis)
        else:
            self.failing = True
            logger.error("alert_send_failed", text=text, retry=True, **diagnosis)
        return SendOutcome.RETRY


def deliver_line(messages: list[str], send: Callable[[str], SendOutcome]) -> bool:
    """Отправить сообщения одной линии; False — временный сбой, линию откатить.

    Первый же ``RETRY`` обрывает отправку: остальное ушло бы в тот же лежащий канал.
    Уже доставленные сообщения линии (у линий по тенантам их бывает несколько) после
    отката уйдут повторно — дубль в канале команды лучше потерянного алерта.
    """
    return all(send(message) is not SendOutcome.RETRY for message in messages)
