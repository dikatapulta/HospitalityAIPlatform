"""Ход гостя — общее ядро гостевых каналов (spec 0027 §2; Task 0017, P-5).

До spec 0027 логика жила в `channels/telegram/guest.py`; вынесена с приходом
второго канала (web): инварианты обязаны жить в одном месте —

- rate-limit ДО оркестратора (spec 0023): превышение отвечает статическим
  отказом БЕЗ вызова LLM, отказ один раз на окно, дальше молча;
- история диалога с окном `MAX_HISTORY_MESSAGES` (#74), гейт подтверждения
  P-9 (`pending_action`), снапшот открытых заявок (spec 0025);
- обещание «зову сотрудника» правдиво (spec 0022): оба пути — `NEEDS_HUMAN`
  и деградация §7.8 — публикуют `conversation.escalated` в outbox ДО реплики;
- привязка созданной заявки к диалогу (`request_origins`, ADR-011).

Транспорт остаётся каналу: он передаёт `reply` — доставку текста гостю
(Telegram шлёт push; web возвращает текст синхронно и/или пишет исходящее).
`rate_limit_key` — тоже выбор канала: telegram — chat_id (spec 0023), web —
stay_id (spec 0027 §3.2: повторный ввод кода не должен обнулять лимиты).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.gateway.api import LlmMessage, LlmProvider
from hospitality.ai.orchestrator import PendingAction
from hospitality.ai.tools.base import ActiveRequest
from hospitality.channels.common.events import publish_escalation
from hospitality.channels.common.models import MessageDirection
from hospitality.channels.common.store import (
    load_conversation_request_ids,
    load_dialog_history,
    load_pending_action,
    record_request_origin,
    set_pending_action,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_rate_limited
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Доставка ответа гостю — транспортная забота канала: (text) -> None.
ReplySender = Callable[[str], Awaitable[None]]

# Вежливый отказ на не-текст (каналы разбирают только текст). Двуязычный: у демо
# 70% гостей — иностранцы (память guest-demographics), а язык гостя без вызова LLM
# здесь неизвестен. Язык-осознанный отказ — по конфигу тенанта, позже.
UNSUPPORTED_REPLY = (
    "Пока я понимаю только текстовые сообщения — напишите, пожалуйста, текстом. "
    "I can only read text messages for now — please send your request as text."
)

# Деградация при недоступности LLM (§7.8): канал отвечает честно и не роняет приём.
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

# Код каталога ошибок (docs/runbooks/errors.md). Лог-код, не AppError: «клиент» —
# гость, он получает вежливый текст, а не HTTP-ошибку. До spec 0027 назывался
# ERR-TELEGRAM-003 (лимит стал общеканальным вместе с этим модулем).
ERR_GUEST_RATE_LIMITED = "ERR-CHANNEL-003"

# Окно дневной ступени: бакеты fixed-window при 86400 выровнены по UTC-суткам
# (epoch кратен суткам) — отдельного механизма «до полуночи» не нужно (spec 0023).
_DAY_SECONDS = 86_400


async def run_guest_turn(
    conversation_id: uuid.UUID,
    guest_text: str,
    inbound_message_id: uuid.UUID,
    *,
    external_id: str,
    rate_limit_key: str,
    reply: ReplySender,
    provider: LlmProvider | None,
) -> None:
    """Обработать текст гостя (внутри `tenant_context`, установленного каналом).

    Вход уже сохранён каналом (`insert_inbound_message`) и дедуплицирован (P-8);
    `external_id` — адрес диалога в канале (уезжает в событие эскалации, чтобы
    персонал знал, откуда гость).
    """
    if await _refuse_if_rate_limited(rate_limit_key, external_id=external_id, reply=reply):
        # Ход дальше не идёт: LLM не вызывается — ровно то, что лимит защищает
        # (issue #41). Входящее уже сохранено каналом — история честная.
        return

    history = _to_llm_history(
        await load_dialog_history(conversation_id, exclude_message_id=inbound_message_id)
    )
    pending = _deserialize_pending(await load_pending_action(conversation_id))
    active_requests = await _load_active_requests(conversation_id)

    try:
        turn = await orchestrator.handle_message(
            message=guest_text,
            history=history,
            pending_action=pending,
            active_requests=active_requests,
            provider=provider,
        )
    except AppError as error:
        # Ошибка провайдера LLM (ERR-AI-001/002/003) — деградация §7.8 забота канала:
        # честный ответ гостю, приём не падает. Эскалация — ДО реплики «уже зову
        # сотрудника», чтобы обещание было правдой (spec 0022).
        logger.warning("guest_turn_degraded", error_code=error.code)
        await publish_escalation(
            conversation_id,
            inbound_message_id,
            chat_id=external_id,
            guest_message=guest_text,
            escalation=EscalationContext(
                reason=EscalationReason.LLM_UNAVAILABLE, error_code=error.code
            ),
        )
        await reply(DEGRADED_REPLY)
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
            chat_id=external_id,
            guest_message=guest_text,
            escalation=turn.escalation,
        )

    logger.info("guest_turn_handled", kind=turn.kind.value)
    await reply(turn.reply_text)


async def _refuse_if_rate_limited(
    rate_limit_key: str, *, external_id: str, reply: ReplySender
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
    key = f"{current_tenant_id()}:{rate_limit_key}"
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
        logger.warning(
            "guest_rate_limited",
            error_code=ERR_GUEST_RATE_LIMITED,
            scope=scope,
            chat_id=external_id,
            count=decision.count,
            limit=decision.limit,
        )
        record_guest_rate_limited(scope)
        # Ответ выбирает только ПЕРВАЯ сработавшая ступень (порядок — от жёсткой
        # к мягкой): когда дневной потолок уже молчит, window-ступень не должна
        # слать «подождите пару минут» на каждое новое окно — ждать-то до завтра
        # (ревью PR #104).
        if not limited and decision.first_rejection:
            reply_text = refusal
        limited = True
    if reply_text is not None:
        await reply(reply_text)
    return limited


async def _load_active_requests(conversation_id: uuid.UUID) -> list[ActiveRequest]:
    """Снапшот открытых заявок диалога для хода оркестратора (spec 0025).

    Граница слоёв: привязка «заявка ↔ диалог» (`request_origins`, ADR-011) —
    знание канала, открытость и данные заявки — модуля requests. Канал склеивает
    их в контракт хода `ActiveRequest`; AI-слой таблиц канала не читает.
    """
    request_ids = await load_conversation_request_ids(conversation_id)
    if not request_ids:
        return []
    open_requests = await requests_api.list_open_requests_by_ids(request_ids)
    return [
        ActiveRequest(
            id=request.id,
            status=request.status,
            summary=request.summary,
            daily_number=request.daily_number,
            room_number=request.room_number,
        )
        for request in open_requests
    ]


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
