"""Факт эскалации к человеку целиком (spec 0022, issue #36, P-6): событие,
durable-строка и счёт — копия канона `platform/events.py`; канал-агностично
(spec 0027 §2).

Три половины одного факта живут в одном файле намеренно: событие
`conversation.escalated` в outbox (доставка), строка `conversation_escalations`
(факт, spec 0035 §6.1) и `count_escalations` (число для сводки дня) обязаны
меняться вместе — писать строку в `store.py`, а публиковать здесь значило бы
разнести «одна эскалация = одна строка» по двум файлам. В `store.py` счёт не
уехал ещё и по R-3: тот файл уже на границе ~400 строк.

Публикует общий ход гостя (`channels/common/guest_turn.py`) на обоих путях
«зову сотрудника»: исход `NEEDS_HUMAN` оркестратора и деградация §7.8 (LLM
недоступен) — из какого бы канала гость ни писал (`chat_id` — external_id
диалога этого канала). Потребляет подписчик
`notifications.notify_staff_on_conversation_escalated` (staff живёт в
Telegram — spec 0026). Событие композиционного слоя: доменные модули его не
знают, шина (`shared/events`) — kernel, слои не нарушены (развилка — spec 0022).

Прямой send из вебхука отвергнут: сбой отправки потерял бы эскалацию молча или
навсегда (500 → ретрай Telegram гасится дедупом `update_id`). Outbox даёт
at-least-once с ретраями воркера (ADR-005/009) и журнал эскалаций. Тем же
доводом issue #209 позже увела в outbox и реплику гостю
(`channels/telegram/redelivery.py`) — «сбой отправки переживается ретраем, а не
записью в лог» оказалось верно для обоих направлений.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import func, select

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.channels.common.models import ConversationEscalation
from hospitality.shared.db import session_scope
from hospitality.shared.events import DomainEvent, publish
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class ConversationEscalated(DomainEvent):
    """Факт «гостю пообещали сотрудника» — персонал обязан узнать (issue #36).

    `inbound_message_id` — id входящего `Message`, породившего эскалацию:
    естественный ключ идемпотентности эффекта (P-8) — повторная доставка
    события не шлёт второе сообщение в staff-чат.
    """

    event_name: ClassVar[str] = "conversation.escalated"

    conversation_id: uuid.UUID
    inbound_message_id: uuid.UUID
    chat_id: str
    guest_message: str
    reason: EscalationReason
    error_code: str
    tool_name: str | None = None
    action_summary: str | None = None
    room_number: str | None = None


async def publish_escalation(
    conversation_id: uuid.UUID,
    inbound_message_id: uuid.UUID,
    *,
    chat_id: str,
    guest_message: str,
    escalation: EscalationContext,
) -> None:
    """Закоммитить факт эскалации: строка `conversation_escalations` + outbox.

    Обе записи — в ОДНОЙ транзакции (spec 0035 §6.1): «эскалация была, но её нет
    в счёте» и «в счёте есть, а персоналу не ушло» — оба расхождения не должны
    возникать вовсе, а не чиниться сверкой. Собственная транзакция на весь
    вызов: бизнес-записи, с которой её можно разделить, нет — сам факт и есть
    нагрузка. Вызывается ДО реплики-обещания гостю (spec 0022): упала
    публикация → гость получил молчание, но не ложь.
    """
    async with session_scope() as session:
        session.add(
            ConversationEscalation(conversation_id=conversation_id, reason=escalation.reason.value)
        )
        await publish(
            session,
            ConversationEscalated(
                conversation_id=conversation_id,
                inbound_message_id=inbound_message_id,
                chat_id=chat_id,
                guest_message=guest_message,
                reason=escalation.reason,
                error_code=escalation.error_code,
                tool_name=escalation.tool_name,
                action_summary=escalation.action_summary,
                room_number=escalation.room_number,
            ),
        )
    logger.info(
        "escalation_published",
        conversation_id=str(conversation_id),
        reason=escalation.reason.value,
        error_code=escalation.error_code,
    )


async def count_escalations(*, created_after: datetime, created_before: datetime) -> int:
    """Сколько раз бот звал сотрудника у текущего тенанта в границах окна.

    Число сводки дня «Бот звал сотрудника: 5 раз» (spec 0035 §6). Владелец
    числа — тот, кто владеет данными: таблица эскалаций живёт здесь, поэтому и
    счёт здесь, а сводка его только складывает с числами других владельцев
    (P-5, §6).

    Границы окна приходят от вызывающей стороны — модуль про часовые пояса
    представления не знает (канон `list_requests_closed_since` в
    `modules/requests`): сутки отеля считает тот, кто показывает сводку. Окно
    полуоткрытое `[created_after, created_before)` — иначе эскалация ровно в
    полночь попала бы сразу в два дня.

    Вызывается внутри `tenant_context`; чужие эскалации отсекает RLS (P-4).
    """
    async with session_scope() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(ConversationEscalation)
            .where(
                ConversationEscalation.created_at >= created_after,
                ConversationEscalation.created_at < created_before,
            )
        )
        return total or 0
