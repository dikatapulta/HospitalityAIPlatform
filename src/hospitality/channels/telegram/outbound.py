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

Реплика с клавиатурой — экран согласия (spec 0029 §4), вопрос персоналу с
ForceReply (spec 0021 П-4, spec 0057 §2) — идёт тем же путём (#296): клавиатура
уезжает в дожим вместе с текстом. Отдельного «best-effort» пути отправки сообщения
в канале нет; best-effort остались тост и правка кнопок под уже отправленным.

Уведомления-подписчики (`notifications.py`) шлют иначе: они уже живут в outbox,
поэтому сбой отправки просто пробрасывают воркеру (at-least-once) — второй раз
класть событие в очередь им незачем.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hospitality.channels.common.store import record_outbound_message
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.redelivery import queue_undelivered_reply
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class ReplyDelivery(StrEnum):
    """Что стало с репликой после `send_reply`."""

    SENT = "sent"  # ушла с первой попытки и записана в историю
    QUEUED = "queued"  # не ушла — встала в дожим, доедет воркером
    LOST = "lost"  # не ушла и не встала в очередь — ERR-TELEGRAM-006


@dataclass(frozen=True)
class ReplyOutcome:
    """Исход `send_reply`; `external_message_id` есть только у `SENT`.

    Большинству вызывающих он не нужен. Нужен тому, кто привязывает к отправленному
    следующий шаг: вопрос персоналу с ForceReply (`staff._ask_note`) отвечает
    нажавшему тостом по исходу и без `message_id` вопроса ответ-реплай не свяжет.
    """

    delivery: ReplyDelivery
    external_message_id: str | None = None


async def send_reply(
    conversation_id: uuid.UUID,
    chat_id: str,
    text: str,
    *,
    sender: TelegramSender,
    correlation_id: str,
    idempotency_key: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> ReplyOutcome:
    """Отправить текст в чат и записать его как исходящий Message.

    Общий для обоих направлений: гостю (`guest.py`) и персоналу в чат службы
    (`staff.py`) — поэтому и событие дожима названо по каналу, а не по адресату
    (`telegram_reply.undelivered`).

    `idempotency_key` — для реплик, у которых есть естественный ключ и повтор
    недопустим: приветствие после согласия (`guest:consent_granted:<диалог>:<версия>`,
    spec 0029 §4). У обычных реплик хода его нет (None): их идемпотентность
    держит дедупликация входящего, а при постановке в очередь дожима ключ
    синтезируется (`queue_undelivered_reply`).

    `reply_markup` — клавиатура Bot API (JSON-совместимый `dict`): inline-кнопка
    экрана согласия, ForceReply вопроса персоналу. Уходит в дожим вместе с
    текстом (#296), иначе дожатое сообщение пришло бы без кнопки, ради которой
    его и слали.
    """
    try:
        sent_id = await sender.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as error:  # не роняем вебхук — но и не теряем реплику
        logger.warning("telegram_send_failed", chat_id=chat_id, error=str(error))
        queued = await queue_undelivered_reply(
            conversation_id,
            chat_id,
            text,
            idempotency_key=idempotency_key,
            reply_markup=reply_markup,
        )
        return ReplyOutcome(ReplyDelivery.QUEUED if queued else ReplyDelivery.LOST)
    await record_outbound_message(
        conversation_id,
        text,
        correlation_id,
        external_message_id=sent_id,
        idempotency_key=idempotency_key,
    )
    return ReplyOutcome(ReplyDelivery.SENT, sent_id)
