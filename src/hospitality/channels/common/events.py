"""Событие эскалации к человеку (spec 0022, issue #36, P-6) — копия канона
`platform/events.py`; канал-агностично (spec 0027 §2).

Публикует общий ход гостя (`channels/common/guest_turn.py`) на обоих путях
«зову сотрудника»: исход `NEEDS_HUMAN` оркестратора и деградация §7.8 (LLM
недоступен) — из какого бы канала гость ни писал (`chat_id` — external_id
диалога этого канала). Потребляет подписчик
`notifications.notify_staff_on_conversation_escalated` (staff живёт в
Telegram — spec 0026). Событие композиционного слоя: доменные модули его не
знают, шина (`shared/events`) — kernel, слои не нарушены (развилка — spec 0022).

Прямой send из вебхука отвергнут: сбой отправки потерял бы эскалацию молча
(best-effort `outbound.py`) или навсегда (500 → ретрай Telegram гасится дедупом
`update_id`). Outbox даёт at-least-once с ретраями воркера (ADR-005/009) и
журнал эскалаций.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from hospitality.ai.escalation import EscalationContext, EscalationReason
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
    """Закоммитить факт эскалации в outbox (внутри `tenant_context`).

    Собственная транзакция: бизнес-записи, с которой её можно разделить, нет —
    сам факт и есть нагрузка. Вызывается ДО реплики-обещания гостю (spec 0022):
    упала публикация → гость получил молчание, но не ложь.
    """
    async with session_scope() as session:
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
