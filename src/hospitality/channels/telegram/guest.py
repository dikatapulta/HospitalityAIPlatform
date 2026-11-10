"""Гостевая ветка Telegram — транспортная обвязка общего хода (spec 0027 §2).

Логика хода (rate-limit 0023 → история/pending/снапшот → оркестратор →
эскалация 0022 → привязка заявки) живёт в `channels/common/guest_turn.py` —
общая для всех гостевых каналов. Здесь остаётся телеграмное: развилка по виду
входящего (CALLBACK/UNSUPPORTED — до общего хода), команды первого касания
(`/start`, `/help` — issue #39, без вызова LLM) и доставка ответа
(`outbound.send_reply` — push с дожимом через outbox при сбое Bot API).

Сюда попадают только сообщения гостя, УЖЕ давшего согласие: consent-gate стоит
выше, в `service.process_update` (spec 0029 §4).

Общий ход получает только гость, привязанный кодом заселения (spec 0074 §2): с
комнатой и Stay из привязки и ключом лимитов `stay_id`, как в вебе. Непривязанный —
auth-only `binding.handle_unbound`, без вызова модели.
"""

from __future__ import annotations

import uuid

from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.consent import normalize_language
from hospitality.channels.common.guest_turn import UNSUPPORTED_REPLY, run_guest_turn
from hospitality.channels.telegram.binding import bind_prompt, handle_unbound
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.consent import acknowledge_consent_click
from hospitality.channels.telegram.greeting import greeting_text, is_first_touch_command
from hospitality.channels.telegram.outbound import send_reply
from hospitality.modules.guests import api as guests_api
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def handle_guest_message(
    conversation_id: uuid.UUID,
    normalized: NormalizedMessage,
    inbound_message_id: uuid.UUID,
    *,
    binding: guests_api.ActiveGuestSession | None,
    sender: TelegramSender,
    provider: LlmProvider | None,
    correlation_id: str,
) -> None:
    """Обработать сообщение гостя (внутри `tenant_context`, установленного каналом).

    `binding` — живая привязка чата к Stay (`binding.resolve_binding`), None — гость
    не привязан: её разрешает `service.process_update` до записи входящего.
    """
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

    language = normalize_language(normalized.actor_language)
    if is_first_touch_command(normalized.text):
        # `/start` и `/help` — детерминированное приветствие без вызова LLM
        # (issue #39): до этого текст команды уезжал в оркестратор и стоил денег
        # на каждом любопытном, нажавшем Start.
        logger.info("guest_first_touch", chat_id=normalized.chat_id, bound=binding is not None)
        greeting = await greeting_text(language)
        if binding is None:
            greeting += "\n\n" + await bind_prompt(language)
        await reply(greeting)
        return

    if binding is None:
        await handle_unbound(conversation_id, normalized, language=language, reply=reply)
        return

    await run_guest_turn(
        conversation_id,
        normalized.text,
        inbound_message_id,
        external_id=normalized.chat_id,
        rate_limit_key=str(binding.stay_id),
        reply=reply,
        provider=provider,
        verified_room_number=binding.room_number,
        verified_stay_id=binding.stay_id,
    )
