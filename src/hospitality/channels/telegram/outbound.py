"""Исходящий ответ в чат: отправка + запись в историю (Task 0017, issue #209).

Общий помощник гостевого (`guest.py`) и служебного (`staff.py`) ответов.
Отправка не роняет обработку вебхука (§8: 500 в ответ Telegram'у ничего не чинит —
повтор апдейта съест дедупликация входящего, P-8), но и не теряется: реплика,
не ушедшая с первой попытки, уходит в outbox и дожимается воркером
(`redelivery.py` — там же решение, его доводы и срок годности реплики).
До issue #209 сбой здесь съедался WARNING'ом — гость видел тишину, и об этом не
узнавал никто; «компромисс Phase 0» кончился вместе с Phase 0.

Недоставленный ответ в историю диалога по-прежнему не пишется (иначе она соврала
бы): `Message` появляется только после фактической отправки — здесь или в дожиме.

Уведомления-подписчики (`notifications.py`) шлют иначе: они уже живут в outbox,
поэтому сбой отправки просто пробрасывают воркеру (at-least-once) — второй раз
класть событие в очередь им незачем.
"""

from __future__ import annotations

import uuid

from hospitality.channels.common.store import record_outbound_message
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.redelivery import queue_undelivered_reply
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def send_reply(
    conversation_id: uuid.UUID,
    chat_id: str,
    text: str,
    *,
    sender: TelegramSender,
    correlation_id: str,
    idempotency_key: str | None = None,
) -> None:
    """Отправить текст в чат и записать его как исходящий Message.

    Общий для обоих направлений: гостю (`guest.py`) и персоналу в чат службы
    (`staff.py`) — поэтому и событие дожима названо по каналу, а не по адресату
    (`telegram_reply.undelivered`).

    `idempotency_key` — для реплик, у которых есть естественный ключ и повтор
    недопустим: приветствие после согласия (`guest:consent_granted:<диалог>:<версия>`,
    spec 0029 §4). У обычных реплик хода его нет (None): их идемпотентность
    держит дедупликация входящего, а при постановке в очередь дожима ключ
    синтезируется (`queue_undelivered_reply`).
    """
    try:
        sent_id = await sender.send_message(chat_id, text)
    except Exception as error:  # не роняем вебхук — но и не теряем реплику
        logger.warning("telegram_send_failed", chat_id=chat_id, error=str(error))
        await queue_undelivered_reply(
            conversation_id, chat_id, text, idempotency_key=idempotency_key
        )
        return
    await record_outbound_message(
        conversation_id,
        text,
        correlation_id,
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
