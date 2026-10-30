"""Ход гостя — общее ядро гостевых каналов (spec 0027 §2; Task 0017, P-5).

До spec 0027 логика жила в `channels/telegram/guest.py`; вынесена с приходом
второго канала (web): инварианты обязаны жить в одном месте —

- ЧП-перехват ПЕРВЫМ действием хода (spec 0034, issue #208): «пожар», «врач»,
  «напали» не доходят ни до лимита, ни до модели — персонал узнаёт эскалацией,
  гость получает утверждённый текст с телефонами;
- rate-limit ДО оркестратора (spec 0023, `chat_limits.py`): превышение
  отвечает статическим отказом БЕЗ вызова LLM, отказ один раз на окно, дальше
  молча (web вместо тишины показывает статус «пауза» — `pause`, issue #222);
  цена хода копится в денежной ступени чата, длинный текст до модели
  не доходит (§5, issue #404);
- история диалога с окном `MAX_HISTORY_MESSAGES` (#74), гейт подтверждения
  P-9 (`pending_action`), снапшот открытых заявок (spec 0025);
- обещание «зову сотрудника» правдиво (spec 0022): оба пути — `NEEDS_HUMAN`
  и деградация §7.8 — публикуют `conversation.escalated` в outbox ДО реплики;
  у отеля со службой эскалаций любая эскалация, ЧП тоже, сначала заводит
  заявку в кабинете (spec 0039 §2, `escalation_request.py`); эскалация, не
  дошедшая до человека, обещания не даёт — гостя ведут на ресепшен (spec 0051);
- вопрос гостя, не покрытый справочником отеля, попадает строкой менеджеру
  (spec 0036 §6) — на любом исходе хода, включая эскалацию, последней записью
  хода: её сбой не отнимает у гостя реплику;
- привязка созданной заявки к диалогу (`request_origins`, ADR-011).

Транспорт остаётся каналу: он передаёт `reply` — доставку текста гостю
(Telegram шлёт push; web возвращает текст синхронно и/или пишет исходящее).
`rate_limit_key` — тоже выбор канала: telegram — chat_id (spec 0023), web —
stay_id (spec 0027 §3.2: повторный ввод кода не должен обнулять лимиты).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from hospitality.ai import orchestrator, urgency
from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.gateway.api import LlmMessage, LlmProvider
from hospitality.ai.orchestrator import PendingAction
from hospitality.ai.tools.base import ActiveRequest
from hospitality.ai.urgency import EmergencyMatch
from hospitality.channels.common.chat_limits import (
    ReplySender,
    record_chat_spend,
    refuse_if_rate_limited,
    refuse_if_too_long,
)
from hospitality.channels.common.escalation_request import escalate
from hospitality.channels.common.models import MessageDirection
from hospitality.channels.common.store import (
    load_conversation_request_ids,
    load_dialog_history,
    load_pending_action,
    record_request_origin,
    set_pending_action,
)
from hospitality.channels.common.unanswered_questions import record_unanswered_question
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import load_tenant_config
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

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

# Эскалацию некому принять — ни карточки в кабинете, ни staff-чата (spec 0051 §3):
# вместо любой реплики-обещания гостя ведут на ресепшен. Канон — текст ЧП
# (`urgency._GO_TO_RECEPTION*`) без «прямо сейчас»: случай не срочный.
# Двуязычный, как DEGRADED_REPLY: язык гостя без модели неизвестен.
_NO_STAFF_DIAL_REPLY = (
    "Извините, сейчас я не могу помочь в чате. Позвоните на ресепшен — наберите "
    "{dial} с телефона в номере — или подойдите к стойке. "
    "Sorry, I can't help in the chat right now. Please call reception — dial "
    "{dial} from your room phone — or go to the front desk."
)
_NO_STAFF_REPLY = (
    "Извините, сейчас я не могу помочь в чате. Позвоните на ресепшен с телефона "
    "в номере или подойдите к стойке. "
    "Sorry, I can't help in the chat right now. Please call reception from your "
    "room phone or go to the front desk."
)

# Код каталога ЧП-перехвата (spec 0034 §4). Не ошибка приложения, а событие,
# которое обязано быть видно в логах и метриках, — тот же класс, что и код
# лимита (`chat_limits.py`): у обоих «клиент» — гость, и оба меняют ход разговора.
ERR_GUEST_EMERGENCY = "ERR-CHANNEL-005"


async def run_guest_turn(
    conversation_id: uuid.UUID,
    guest_text: str,
    inbound_message_id: uuid.UUID,
    *,
    external_id: str,
    rate_limit_key: str,
    reply: ReplySender,
    # Статус паузы лимита вместо тишины (`refuse_if_rate_limited`, #222); Telegram — None.
    pause: ReplySender | None = None,
    provider: LlmProvider | None,
    verified_room_number: str | None = None,
) -> None:
    """Обработать текст гостя (внутри `tenant_context`, установленного каналом).

    Вход уже сохранён каналом (`insert_inbound_message`) и дедуплицирован (P-8);
    `external_id` — адрес диалога в канале (уезжает в событие эскалации, чтобы
    персонал знал, откуда гость). `verified_room_number` — комната из привязки
    канала (web: Stay сессии, spec 0027 §3.2); Telegram передаёт None.
    """
    emergency = urgency.detect_emergency(guest_text)
    if emergency is not None:
        # ЧП: ход обрывается ДО лимита и ДО модели (spec 0034 §4).
        await _intercept_emergency(
            conversation_id,
            inbound_message_id,
            emergency,
            guest_text=guest_text,
            external_id=external_id,
            reply=reply,
            verified_room_number=verified_room_number,
        )
        return

    if await refuse_if_rate_limited(
        rate_limit_key, external_id=external_id, reply=reply, pause=pause
    ):
        # Ход дальше не идёт: LLM не вызывается — ровно то, что лимит защищает
        # (issue #41). Входящее уже сохранено каналом — история честная.
        return
    if await refuse_if_too_long(guest_text, external_id=external_id, reply=reply):
        # Предел длины (spec 0023 §5): в историю модели такое входящее тоже
        # не попадёт (`load_dialog_history`).
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
            verified_room_number=verified_room_number,
            provider=provider,
        )
    except AppError as error:
        # Ошибка провайдера LLM (ERR-AI-001/002/003) — деградация §7.8 забота канала:
        # честный ответ гостю, приём не падает. Эскалация — ДО реплики «уже зову
        # сотрудника», чтобы обещание было правдой (spec 0022).
        logger.warning("guest_turn_degraded", error_code=error.code)
        staff_reached = await escalate(
            conversation_id,
            inbound_message_id,
            chat_id=external_id,
            guest_message=guest_text,
            escalation=EscalationContext(
                reason=EscalationReason.LLM_UNAVAILABLE,
                error_code=error.code,
                # Комната из привязки (web): персоналу есть куда прийти к гостю,
                # у которого нет Telegram-чата (spec 0027 §3.2).
                room_number=verified_room_number,
            ),
            verified_room_number=verified_room_number,
        )
        await reply(
            DEGRADED_REPLY
            if staff_reached
            else await _no_staff_reply(conversation_id, EscalationReason.LLM_UNAVAILABLE)
        )
        return

    # Цена — ДО остальных шагов хода: деньги уже потрачены, что бы ни упало ниже.
    await record_chat_spend(rate_limit_key, turn.llm_cost_usd)

    # Гейт P-9: сохранить/очистить ожидание подтверждения (None на всех исходах,
    # кроме AWAITING_CONFIRMATION — тем самым ожидание само гасится после исполнения).
    await set_pending_action(conversation_id, _serialize_pending(turn.pending_action))

    if turn.created_request_id is not None:
        # Привязать заявку к диалогу — по ней подписчик вернёт гостю подтверждение
        # о выполнении (ADR-011). Уведомление службе идёт подписчиком request.created.
        await record_request_origin(turn.created_request_id, conversation_id)

    reply_text = turn.reply_text
    if turn.escalation is not None:
        # NEEDS_HUMAN (инвариант OrchestratorTurn): факт «гостю нужен человек» —
        # в outbox ДО реплики-обещания «подключу сотрудника» (spec 0022).
        escalation = turn.escalation
        if escalation.room_number is None and verified_room_number is not None:
            # Комната из привязки надёжнее отсутствия комнаты из аргументов
            # инструмента (spec 0027 §3.2): web-гостя иначе не найти.
            escalation = replace(escalation, room_number=verified_room_number)
        staff_reached = await escalate(
            conversation_id,
            inbound_message_id,
            chat_id=external_id,
            guest_message=guest_text,
            escalation=escalation,
            verified_room_number=verified_room_number,
        )
        if not staff_reached:
            # Реплика хода обещает человека — статическая или текст модели из
            # `escalate_to_staff`; звать некому, и она заменяется целиком (spec 0051).
            reply_text = await _no_staff_reply(conversation_id, escalation.reason)

    # Цена и чат в одной строке — виновный в расходе чат находится по логам,
    # хотя журнал шлюза диалога не знает (#236, spec 0023 §5).
    logger.info(
        "guest_turn_handled",
        kind=turn.kind.value,
        chat_id=external_id,
        limit_key=rate_limit_key,
        llm_cost_usd=str(turn.llm_cost_usd),
    )
    await reply(reply_text)

    if turn.unanswered_question is not None:
        # Справочник отеля не покрыл вопрос гостя (spec 0036 §6): строка —
        # менеджеру на страницу «Справочник отеля». Пишет её канал, буква в букву
        # канон `escalation` выше; эскалацией это НЕ является — персонал ничего
        # не получает.
        #
        # ПОСЛЕДНЕЙ записью хода: упасть может любая запись, а повтор доставки
        # гасит дедуп входящего. Потеря строки стоит одного вопроса в списке
        # менеджера; стой она до реплики — её сбой оставил бы гостя без ответа,
        # а на AWAITING_CONFIRMATION ещё и с гейтом, взведённым на вопрос,
        # которого гость не видел (ревью PR #344).
        await record_unanswered_question(conversation_id, turn.unanswered_question)


async def _intercept_emergency(
    conversation_id: uuid.UUID,
    inbound_message_id: uuid.UUID,
    emergency: EmergencyMatch,
    *,
    guest_text: str,
    external_id: str,
    reply: ReplySender,
    verified_room_number: str | None,
) -> None:
    """ЧП: эскалация персоналу + утверждённый статический ответ (spec 0034).

    Модель не вызывается вовсе — в этом и смысл: путь «гость написал о пожаре →
    человек узнал» не должен зависеть ни от доступности провайдера, ни от
    дневного бюджета тенанта. Реплика уходит и при упавшей публикации (#382,
    исключение из инварианта spec 0022): текст перехвата ничего не обещает от
    имени персонала, а отправляет гостя на ресепшен, так что лжи в нём нет, а
    молчание на «пожар» — худший исход. Исключение публикации после реплики
    летит дальше, как раньше. Адресат сообщения в Telegram — дефолтный чат тенанта: категории
    у эскалации нет (spec 0026), а ЧП — ровно тот случай, ради которого «уровень
    выше» и существует. У отеля со службой эскалаций ЧП вдобавок становится
    срочной заявкой в ней — карточкой в кабинете (spec 0039 §2).
    """
    logger.warning(
        "emergency_intercepted",
        error_code=ERR_GUEST_EMERGENCY,
        kind=emergency.kind.value,
        language=emergency.language,
        conversation_id=str(conversation_id),
        chat_id=external_id,
    )
    try:
        await escalate(
            conversation_id,
            inbound_message_id,
            chat_id=external_id,
            guest_message=guest_text,
            escalation=EscalationContext(
                reason=EscalationReason.EMERGENCY,
                error_code=ERR_GUEST_EMERGENCY,
                room_number=verified_room_number,
            ),
            verified_room_number=verified_room_number,
        )
    finally:
        phone, room_dial = await _reception_contacts()
        await reply(
            urgency.emergency_reply(emergency.language, phone, reception_room_dial=room_dial)
        )


async def _no_staff_reply(conversation_id: uuid.UUID, reason: EscalationReason) -> str:
    """Реплика вместо обещания сотрудника: эскалацию некому принять (spec 0051 §3).

    INFO, а не WARNING: у отеля без бота и без службы эскалаций (гостевая копия
    до этапа 3) это штатное состояние. Сбой карточки у отеля с ключом уже
    записан шагом заявки как `escalation_request_skipped` уровня WARNING.
    """
    logger.info(
        "escalation_without_staff", reason=reason.value, conversation_id=str(conversation_id)
    )
    phone, room_dial = await _reception_contacts()
    return no_staff_reply(phone, room_dial)


def no_staff_reply(reception_phone: str | None, reception_room_dial: str | None) -> str:
    """Текст «позвоните на ресепшен»; без цифры — фраза без неё, без телефона — без строки 📞.

    Поля и деградация — как у текста ЧП (`urgency.emergency_reply`): текст
    действует и без обоих.
    """
    text = (
        _NO_STAFF_DIAL_REPLY.format(dial=reception_room_dial)
        if reception_room_dial
        else _NO_STAFF_REPLY
    )
    if reception_phone:
        text += f"\n📞 Ресепшен / Reception: {reception_phone}"
    return text


async def _reception_contacts() -> tuple[str | None, str | None]:
    """Телефон ресепшена и цифра из номера — для текстов «на ресепшен»; без конфига — (None, None).

    Читается только на сработавшем перехвате и на эскалации без адресата (spec
    0051), а не каждый ход: платить SELECT'ом
    за сообщение «принесите полотенце» незачем. Деградация та же, что у подсказок
    служб (`ai/tools/registry.py`): без обоих полей текст остаётся действенным —
    «позвоните с телефона в номере или подойдите к стойке» от настроек не зависит.
    """
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        logger.warning("reception_phone_unavailable", error_code=error.code)
        return None, None
    return config.reception_phone, config.reception_room_dial


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
