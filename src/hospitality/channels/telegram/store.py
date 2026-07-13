"""Персистентность диалога Telegram (Task 0016, P-4, P-8).

Единственный путь записать Conversation/Message. Каждая функция — своя транзакция
по канону P-4/P-12: вызывается внутри `tenant_context`, открывает `session_scope()`
(RLS проставляет tenant_id сама). Идемпотентность входящих (P-8) держит уникальное
ограничение `messages(tenant_id, idempotency_key)`, а не проверка-перед-вставкой:
между SELECT и INSERT возможна гонка двух доставок одного апдейта, БД её закрывает.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hospitality.channels.base import NormalizedMessage
from hospitality.channels.telegram.models import (
    Conversation,
    Message,
    MessageContentKind,
    MessageDirection,
)
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def ensure_conversation(external_id: str) -> uuid.UUID:
    """id диалога по чату гостя; создаёт его при первом сообщении (идемпотентно).

    Гонка двух первых сообщений одного чата закрывается уникальным ограничением
    `(tenant_id, channel, external_id)`: проигравший INSERT падает, повторный SELECT
    находит созданную строку.
    """
    async with session_scope() as session:
        existing = await session.scalar(
            select(Conversation.id).where(
                Conversation.channel == CHANNEL, Conversation.external_id == external_id
            )
        )
        if existing is not None:
            return existing
        conversation = Conversation(channel=CHANNEL, external_id=external_id)
        session.add(conversation)
        try:
            await session.flush()
        except IntegrityError:
            # Диалог создала параллельная доставка между SELECT и INSERT — берём её.
            await session.rollback()
            found = await session.scalar(
                select(Conversation.id).where(
                    Conversation.channel == CHANNEL, Conversation.external_id == external_id
                )
            )
            if found is None:  # pragma: no cover — IntegrityError без строки невозможен
                raise
            return found
        return conversation.id


async def insert_inbound_message(
    conversation_id: uuid.UUID, message: NormalizedMessage, correlation_id: str
) -> uuid.UUID | None:
    """Сохранить входящее сообщение; None — дубликат доставки (второго Message нет, P-8).

    Дубликат распознаётся по нарушению уникальности `(tenant_id, idempotency_key)`:
    повторный вебхук с тем же update_id не создаёт вторую строку и не влечёт второй
    ответ гостю.
    """
    try:
        async with session_scope() as session:
            row = Message(
                conversation_id=conversation_id,
                direction=MessageDirection.INBOUND,
                content_kind=MessageContentKind(message.kind.value),
                text=message.text,
                external_message_id=message.external_message_id,
                idempotency_key=message.idempotency_key,
                correlation_id=correlation_id,
            )
            session.add(row)
            await session.flush()
            message_id = row.id
    except IntegrityError as error:
        # Имя ограничения по NAMING_CONVENTION (shared/db.py): uq_<table>_<column_0>.
        if "uq_messages_tenant_id" not in str(error):
            raise
        return None
    return message_id


async def record_outbound_message(
    conversation_id: uuid.UUID,
    text: str,
    correlation_id: str,
    *,
    external_message_id: str | None,
) -> uuid.UUID:
    """Сохранить исходящий ответ платформы (идемпотентности не требует: см. models)."""
    async with session_scope() as session:
        row = Message(
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            content_kind=MessageContentKind.TEXT,
            text=text,
            external_message_id=external_message_id,
            correlation_id=correlation_id,
        )
        session.add(row)
        await session.flush()
        return row.id
