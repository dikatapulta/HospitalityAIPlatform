"""Гостевая ветка Telegram — транспортная обвязка общего хода (spec 0027 §2).

Логика хода (rate-limit 0023 → история/pending/снапшот → оркестратор →
эскалация 0022 → привязка заявки) живёт в `channels/common/guest_turn.py` —
общая для всех гостевых каналов. Здесь остаётся телеграмное: развилка по виду
входящего (CALLBACK/UNSUPPORTED — до общего хода), команды первого касания
(`/start`, `/help` — issue #39, без вызова LLM) и доставка ответа
(`outbound.send_reply` — push с дожимом через outbox при сбое Bot API).

Сюда попадают только сообщения гостя, УЖЕ давшего согласие: consent-gate стоит
выше, в `service.process_update` (spec 0029 §4).

Ключ rate-limit — chat_id (spec 0023): телеграм-гость и есть его чат.
"""

from __future__ import annotations

import uuid

from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.consent import normalize_language
from hospitality.channels.common.guest_turn import UNSUPPORTED_REPLY, run_guest_turn
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.consent import acknowledge_consent_click
from hospitality.channels.telegram.greeting import greeting_text, is_first_touch_command
from hospitality.channels.telegram.outbound import send_reply
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def handle_guest_message(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    inbound_message_id: uuid.UUID,
    *,
    sender: TelegramSender,
    provider: LlmProvider | None,
    correlation_id: str,
) -> None:
    """Обработать сообщение гостя (внутри `tenant_context`, установленного каналом)."""
    if normalized.kind is MessageKind.CALLBACK:
        # Единственная кнопка гостевого чата — согласие (spec 0029), и сюда её
        # нажатие доходит лишь у гостя, который уже согласился (старое сообщение
        # с кнопкой): гасим «часики» тостом. Всё прочее — чужая/старая
        # клавиатура, оборонительный путь.
        await acknowledge_consent_click(normalized, sender=sender)
        logger.info("guest_callback_ignored")
        return
    if normalized.kind is MessageKind.UNSUPPORTED:
        await send_reply(
            conversation_id,
            normalized.chat_id,
            UNSUPPORTED_REPLY,
            sender=sender,
            correlation_id=correlation_id,
        )
        return
    if normalized.text is None:  # pragma: no cover — контракт нормализации: TEXT ⇒ text
        return

    async def reply(text: str) -> None:
        await send_reply(
            conversation_id, normalized.chat_id, text, sender=sender, correlation_id=correlation_id
        )

    if is_first_touch_command(normalized.text):
        # `/start` и `/help` — детерминированное приветствие без вызова LLM
        # (issue #39): до этого текст команды уезжал в оркестратор и стоил денег
        # на каждом любопытном, нажавшем Start.
        logger.info("guest_first_touch", chat_id=normalized.chat_id)
        await reply(await greeting_text(normalize_language(normalized.actor_language)))
        return

    await run_guest_turn(
        conversation_id,
        normalized.text,
        inbound_message_id,
        external_id=normalized.chat_id,
        rate_limit_key=normalized.chat_id,
        reply=reply,
        provider=provider,
    )
