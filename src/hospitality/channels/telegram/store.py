"""Персистентность диалога Telegram (Task 0016, P-4, P-8).

Единственный путь записать Conversation/Message. Каждая функция — своя транзакция
по канону P-4/P-12: вызывается внутри `tenant_context`, открывает `session_scope()`
(RLS проставляет tenant_id сама). Идемпотентность входящих (P-8) держит уникальное
ограничение `messages(tenant_id, idempotency_key)`, а не проверка-перед-вставкой:
между SELECT и INSERT возможна гонка двух доставок одного апдейта, БД её закрывает.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hospitality.channels.base import NormalizedMessage
from hospitality.channels.telegram.models import (
    Conversation,
    Message,
    MessageContentKind,
    MessageDirection,
    RequestOrigin,
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
    idempotency_key: str | None = None,
) -> uuid.UUID:
    """Сохранить исходящий ответ платформы.

    Реплики гостю идемпотентности не требуют (`idempotency_key=None`, NULL —
    Postgres считает NULL-и различными). Уведомления-подписчики (Task 0017,
    P-8) передают естественный ключ (`staff:request_created:<id>`,
    `guest:request_done:<id>`) — повторная доставка события не создаёт второй
    строки: конфликт по `(tenant_id, idempotency_key)` виден вызывающему.
    """
    async with session_scope() as session:
        row = Message(
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            content_kind=MessageContentKind.TEXT,
            text=text,
            external_message_id=external_message_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        session.add(row)
        await session.flush()
        return row.id


async def load_pending_action(conversation_id: uuid.UUID) -> dict[str, Any] | None:
    """Прочитать состояние гейта P-9 диалога (Task 0017); None — ожидания нет."""
    async with session_scope() as session:
        return await session.scalar(
            select(Conversation.pending_action).where(Conversation.id == conversation_id)
        )


async def set_pending_action(
    conversation_id: uuid.UUID, pending_action: dict[str, Any] | None
) -> None:
    """Записать/очистить состояние гейта P-9 диалога (Task 0017)."""
    async with session_scope() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:  # pragma: no branch — диалог только что создан
            conversation.pending_action = pending_action


# Сколько прошлых реплик отдаём модели как контекст. Окно намеренно ограничено
# (баг #71, находка на staging): без лимита длинный диалог (1) тянет модель
# имитировать собственные прошлые ошибки — в т.ч. галлюцинацию «заявка принята»
# без вызова инструмента; (2) безгранично растит стоимость хода (input-токены);
# (3) однажды упирается в лимит контекста. ~20 сообщений ≈ 10 ходов — достаточно
# для связного диалога консьержа, состояние подтверждения P-9 живёт отдельно
# (conversations.pending_action), а не в этой истории.
MAX_HISTORY_MESSAGES = 20


async def load_dialog_history(
    conversation_id: uuid.UUID, *, exclude_message_id: uuid.UUID
) -> list[tuple[MessageDirection, str]]:
    """Прежние текстовые реплики диалога для контекста оркестратора (Task 0017).

    Текущее входящее исключается по `exclude_message_id` (оно уже сохранено, но
    оркестратор добавит его сам). Не-текст (`unsupported`, NULL text) пропускается.
    Отдаются последние `MAX_HISTORY_MESSAGES` реплик (свежий хвост берём через
    `DESC + LIMIT`), но в хронологическом порядке — как история диалога.
    """
    async with session_scope() as session:
        rows = await session.execute(
            select(Message.direction, Message.text)
            .where(
                Message.conversation_id == conversation_id,
                Message.id != exclude_message_id,
                Message.content_kind == MessageContentKind.TEXT,
                Message.text.is_not(None),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(MAX_HISTORY_MESSAGES)
        )
        recent = [(direction, text) for direction, text in rows if text is not None]
        recent.reverse()
        return recent


async def record_request_origin(request_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    """Привязать заявку к диалогу-источнику (Task 0017, ADR-011); идемпотентно.

    Повторная запись того же `request_id` (пере-обработка апдейта) конфликтует по
    `(tenant_id, request_id)` — берётся уже записанная привязка, второй строки нет.
    """
    try:
        async with session_scope() as session:
            session.add(RequestOrigin(request_id=request_id, conversation_id=conversation_id))
            await session.flush()
    except IntegrityError as error:
        if "uq_request_origins_tenant_id" not in str(error):
            raise
        logger.info("request_origin_already_recorded", request_id=str(request_id))


async def load_request_origin_conversation(request_id: uuid.UUID) -> uuid.UUID | None:
    """id диалога-источника заявки (Task 0017); None — привязки нет (заявка не из чата)."""
    async with session_scope() as session:
        conversation_id: uuid.UUID | None = await session.scalar(
            select(RequestOrigin.conversation_id).where(RequestOrigin.request_id == request_id)
        )
    return conversation_id


async def load_conversation_external_id(conversation_id: uuid.UUID) -> str | None:
    """external_id (chat_id провайдера) диалога (Task 0017); None — диалога нет."""
    async with session_scope() as session:
        external_id: str | None = await session.scalar(
            select(Conversation.external_id).where(Conversation.id == conversation_id)
        )
    return external_id


async def notification_already_sent(idempotency_key: str) -> bool:
    """Уведомление с этим ключом уже отправлено (P-8, Task 0017)?

    Ключ уникален в паре с tenant_id (ограничение messages); RLS ограничивает
    видимость текущим тенантом. Опора идемпотентности подписчиков при повторной
    доставке события.
    """
    async with session_scope() as session:
        existing = await session.scalar(
            select(Message.id).where(Message.idempotency_key == idempotency_key)
        )
    return existing is not None
