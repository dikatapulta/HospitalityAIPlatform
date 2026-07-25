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

Обещание «зову сотрудника» правдиво (spec 0022, issue #36): оба пути — исход
`NEEDS_HUMAN` и деградация §7.8 — публикуют `conversation.escalated` в outbox
ДО реплики гостю. Упала публикация → гость получил молчание, но не ложь; факт,
закоммиченный в outbox, воркер доставит в staff-чат с ретраями (ADR-005).

Rate-limit гостевого чата (issue #41, spec 0023): две ступени на chat_id
(всплеск + дневной потолок, счётчики — канон `shared/ratelimit`) проверяются
ДО оркестратора — превышение отвечает статическим отказом БЕЗ вызова LLM,
защищая общий дневной бюджет тенанта (ERR-AI-002) от одного болтливого чата.
Входящее при этом уже сохранено — история диалога честная.
"""

from __future__ import annotations

import uuid
from typing import Any

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.gateway.api import LlmMessage, LlmProvider
from hospitality.ai.orchestrator import PendingAction
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.events import publish_escalation
from hospitality.channels.telegram.models import MessageDirection
from hospitality.channels.telegram.outbound import send_reply
from hospitality.channels.telegram.store import (
    load_dialog_history,
    load_pending_action,
    record_request_origin,
    set_pending_action,
)
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import current_tenant_id

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

# Отказы rate-limit (issue #41, spec 0023): статические и двуязычные — как
# UNSUPPORTED_REPLY, язык гостя без LLM неизвестен, а звать LLM ради отказа
# значило бы жечь тот самый бюджет, который лимит защищает. Тексты честные:
# «пара минут» — только у короткого окна, у дневного потолка ждать до завтра.
RATE_LIMITED_REPLY = (
    "Сообщений слишком много подряд — мне нужна небольшая пауза. Подождите, "
    "пожалуйста, пару минут или обратитесь к сотруднику отеля. "
    "Too many messages in a row — I need a short pause. Please wait a couple "
    "of minutes or ask a hotel staff member."
)
DAILY_LIMIT_REPLY = (
    "На сегодня лимит сообщений в этом чате исчерпан. Пожалуйста, обратитесь "
    "к сотруднику отеля — вам помогут. "
    "This chat has reached its daily message limit. Please contact a hotel "
    "staff member — they will help you."
)

# Код каталога ошибок (docs/runbooks/errors.md). Лог-код, не AppError — как
# ERR-TELEGRAM-002: вебхук отвечает Telegram 200, «клиент» здесь — гость,
# и он получает вежливый текст, а не HTTP-ошибку.
ERR_GUEST_RATE_LIMITED = "ERR-TELEGRAM-003"

# Окно дневной ступени: бакеты fixed-window при 86400 выровнены по UTC-суткам
# (epoch кратен суткам) — отдельного механизма «до полуночи» не нужно (spec 0023).
_DAY_SECONDS = 86_400


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
        # Кнопок в гостевом чате нет (Phase 0) — нажать нечего; оборонительный
        # путь на случай чужого/старого сообщения с клавиатурой.
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

    if await _refuse_if_rate_limited(
        conversation_id, normalized.chat_id, sender=sender, correlation_id=correlation_id
    ):
        # Ход дальше не идёт: LLM не вызывается — ровно то, что лимит защищает
        # (issue #41). Входящее уже сохранено каналом — история честная.
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
        # Эскалация — ДО реплики «уже зову сотрудника», чтобы обещание было правдой.
        logger.warning("guest_turn_degraded", error_code=error.code)
        await publish_escalation(
            conversation_id,
            inbound_message_id,
            chat_id=normalized.chat_id,
            guest_message=normalized.text,
            escalation=EscalationContext(
                reason=EscalationReason.LLM_UNAVAILABLE, error_code=error.code
            ),
        )
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

    if turn.escalation is not None:
        # NEEDS_HUMAN (инвариант OrchestratorTurn): факт «гостю нужен человек» —
        # в outbox ДО реплики-обещания «подключу сотрудника» (spec 0022).
        await publish_escalation(
            conversation_id,
            inbound_message_id,
            chat_id=normalized.chat_id,
            guest_message=normalized.text,
            escalation=turn.escalation,
        )

    logger.info("guest_turn_handled", kind=turn.kind.value)
    await send_reply(
        conversation_id,
        normalized.chat_id,
        turn.reply_text,
        sender=sender,
        correlation_id=correlation_id,
    )


async def _refuse_if_rate_limited(
    conversation_id: uuid.UUID,
    chat_id: str,
    *,
    sender: TelegramSender,
    correlation_id: str,
) -> bool:
    """Ступени rate-limit ДО оркестратора (spec 0023): True — ход не продолжается.

    Обе ступени инкрементятся на каждый текст гостя: сообщения, отклонённые
    всплеск-ступенью, продолжают наполнять дневной счётчик — иначе спамер
    получал бы свежий дневной запас после каждого окна. Отказ отправляется
    один раз на окно (`first_rejection`), дальше — молча: N сообщений спама
    не должны рождать N исходящих. Redis недоступен → fail-open на весь ход.
    """
    settings = get_settings()
    # tenant_id в ключе обязателен (P-4): у Redis нет RLS, изоляция тенантов
    # держится дисциплиной ключей канона consume_rate_limit.
    key = f"{current_tenant_id()}:{chat_id}"
    tiers = (
        # Дневная ступень первой: при одновременном срабатывании гость должен
        # услышать более жёсткую правду («сегодня всё»), а не «пару минут».
        (
            "guest_chat_daily",
            settings.guest_chat_rate_limit_messages_per_day,
            _DAY_SECONDS,
            DAILY_LIMIT_REPLY,
        ),
        (
            "guest_chat_window",
            settings.guest_chat_rate_limit_messages,
            settings.guest_chat_rate_limit_window_seconds,
            RATE_LIMITED_REPLY,
        ),
    )
    limited = False
    reply_text: str | None = None
    for scope, limit, window_seconds, refusal in tiers:
        if limit <= 0:
            # Ступень отключена конфигурацией — страховочный люк (spec 0023).
            continue
        decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
        if not decision.available:
            # Fail-open (spec 0023, раздел 2): вторую ступень не проверяем,
            # чтобы гость не ждал таймаут Redis дважды за один ход.
            break
        if decision.allowed:
            continue
        limited = True
        logger.warning(
            "guest_rate_limited",
            error_code=ERR_GUEST_RATE_LIMITED,
            scope=scope,
            chat_id=chat_id,
            count=decision.count,
            limit=decision.limit,
        )
        record_guest_rate_limited(scope)
        if reply_text is None and decision.first_rejection:
            reply_text = refusal
    if reply_text is not None:
        await send_reply(
            conversation_id, chat_id, reply_text, sender=sender, correlation_id=correlation_id
        )
    return limited


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
