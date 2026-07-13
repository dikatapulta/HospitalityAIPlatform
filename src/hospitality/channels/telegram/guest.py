"""Гостевой ход диалога: канал зовёт оркестратор (Task 0017, P-5, ADR-011).

На текстовое сообщение гостя канал вызывает `ai.orchestrator.handle_message` и
отвечает `reply_text`. Между двумя вебхуками (гость просит → «оформить?» → гость
«да») канал хранит состояние, которого требует оркестратор: историю диалога
(восстанавливается из `Message`) и `pending_action` — гейт подтверждения P-9
(колонка `conversations.pending_action`). Бизнес-логики нет: создание заявки живёт
в `modules/requests`, оркестратор лишь её вызывает (P-5).

Заявка создана (`ACTION_DONE`) → канал записывает привязку `request_origins`
(куда вернуть подтверждение), а уведомление службе шлёт подписчик события
`request.created` (`notifications.py`), НЕ этот код (P-6).
"""

from __future__ import annotations

import uuid
from typing import Any

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import LlmMessage, LlmProvider
from hospitality.ai.orchestrator import PendingAction
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.models import MessageDirection
from hospitality.channels.telegram.outbound import send_reply
from hospitality.channels.telegram.store import (
    load_dialog_history,
    load_pending_action,
    record_request_origin,
    set_pending_action,
)
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Вежливый отказ на не-текст (Phase 0 разбирает только текст). Двуязычный: у демо
# 70% гостей — иностранцы (память guest-demographics), а язык гостя без вызова LLM
# здесь неизвестен. Язык-осознанный отказ — Phase 1 (по конфигу тенанта/оркестратору).
UNSUPPORTED_REPLY = (
    "Пока я понимаю только текстовые сообщения — напишите, пожалуйста, текстом. "
    "I can only read text messages for now — please send your request as text."
)

# Деградация при недоступности LLM (§7.8): канал отвечает честно и не роняет вебхук.
# Структурные формы/кнопки («заявка», «позвать сотрудника») — Phase 1.
DEGRADED_REPLY = (
    "Извините, сейчас я не могу ответить — уже зову сотрудника отеля. "
    "Sorry, I can't respond right now — I'm calling a staff member for you."
)


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

    history = _to_llm_history(
        await load_dialog_history(conversation_id, exclude_message_id=inbound_message_id)
    )
    pending = _deserialize_pending(await load_pending_action(conversation_id))

    try:
        turn = await orchestrator.handle_message(
            message=normalized.text,
            history=history,
            pending_action=pending,
            provider=provider,
        )
    except AppError as error:
        # Ошибка провайдера LLM (ERR-AI-001/002/003) — деградация §7.8 забота канала:
        # честный ответ гостю, вебхук отвечает 200 (не зацикливать ретраи Telegram).
        logger.warning("guest_turn_degraded", error_code=error.code)
        await send_reply(
            conversation_id,
            normalized.chat_id,
            DEGRADED_REPLY,
            sender=sender,
            correlation_id=correlation_id,
        )
        return

    # Гейт P-9: сохранить/очистить ожидание подтверждения (None на всех исходах,
    # кроме AWAITING_CONFIRMATION — тем самым ожидание само гасится после исполнения).
    await set_pending_action(conversation_id, _serialize_pending(turn.pending_action))

    if turn.created_request_id is not None:
        # Привязать заявку к диалогу — по ней подписчик вернёт гостю подтверждение
        # о выполнении (ADR-011). Уведомление службе идёт подписчиком request.created.
        await record_request_origin(turn.created_request_id, conversation_id)

    logger.info("guest_turn_handled", kind=turn.kind.value)
    await send_reply(
        conversation_id,
        normalized.chat_id,
        turn.reply_text,
        sender=sender,
        correlation_id=correlation_id,
    )


def _to_llm_history(rows: list[tuple[MessageDirection, str]]) -> list[LlmMessage]:
    """Реплики истории → формат оркестратора: inbound → user, outbound → assistant."""
    return [
        LlmMessage(
            role="user" if direction is MessageDirection.INBOUND else "assistant",
            content=text,
        )
        for direction, text in rows
    ]


def _serialize_pending(pending: PendingAction | None) -> dict[str, Any] | None:
    if pending is None:
        return None
    return {"tool_name": pending.tool_name, "arguments": pending.arguments}


def _deserialize_pending(data: dict[str, Any] | None) -> PendingAction | None:
    if data is None:
        return None
    return PendingAction(tool_name=data["tool_name"], arguments=data["arguments"])
