"""Исходящий ответ в чат: best-effort отправка + запись в историю (Task 0017).

Общий помощник гостевого (`guest.py`) и служебного (`staff.py`) ответов. Отправка
best-effort (§8): сбой сети логируется, но не роняет обработку вебхука и не
записывает недоставленный ответ (иначе история диалога соврала бы). Telegram
повторит доставку апдейта; входящее к тому моменту дедуплицировано (P-8), второго
ответа не будет — компромисс Phase 0, тот же, что в Task 0016.

Уведомления-подписчики (`notifications.py`) шлют иначе: с дедупликацией по ключу и
пробросом ошибки воркеру для ретрая (at-least-once), поэтому здесь их пути нет.
"""

from __future__ import annotations

import uuid

from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.store import record_outbound_message
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def send_reply(
    conversation_id: uuid.UUID,
    chat_id: str,
    text: str,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Отправить текст в чат и записать его как исходящий Message (best-effort)."""
    try:
        sent_id = await sender.send_message(chat_id, text)
    except Exception as error:  # best-effort: сбой отправки не роняет приём вебхука
        logger.warning("telegram_send_failed", chat_id=chat_id, error=str(error))
        return
    await record_outbound_message(
        conversation_id, text, correlation_id, external_message_id=sent_id
    )
