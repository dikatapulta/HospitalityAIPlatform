"""Диалоги веб-чата для кабинета — раздел «Диалоги» (spec 0067 §2, issue #767).

Только чтение. Таблицы диалога общие для каналов (`channels/common/models.py`),
а раздел показывает лишь веб-чат (§2.1): под каналом `telegram` рядом с гостями
лежат служебные чаты персонала. Поэтому чтение живёт в канале web и каждый запрос
режет по `CHANNEL`. Каждая функция — своя транзакция под RLS тенанта запроса
(канон `channels/common/store.py`): диалог другого отеля запрос не находит.
Наружу — замороженные dataclass, не ORM.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from hospitality.channels.common.models import (
    Conversation,
    ConversationEscalation,
    Message,
    MessageContentKind,
    MessageDirection,
    RequestOrigin,
)
from hospitality.channels.web.service import CHANNEL
from hospitality.shared.db import session_scope


@dataclass(frozen=True)
class MessageRow:
    """Сообщение ленты. `text` None — гость прислал не текст."""

    direction: MessageDirection
    text: str | None
    created_at: datetime


@dataclass(frozen=True)
class ConversationRow:
    """Строка списка: диалог и его последнее сообщение."""

    id: uuid.UUID
    guest_identity_id: uuid.UUID | None
    last_message: MessageRow


@dataclass(frozen=True)
class ConversationHead:
    """Шапка ленты."""

    id: uuid.UUID
    guest_identity_id: uuid.UUID | None
    created_at: datetime


def _message(
    direction: MessageDirection, kind: MessageContentKind, text: str | None, created_at: datetime
) -> MessageRow:
    return MessageRow(
        direction=direction,
        text=text if kind is MessageContentKind.TEXT else None,
        created_at=created_at,
    )


async def list_web_conversations(
    *,
    identity_ids: Collection[uuid.UUID] | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[ConversationRow]:
    """Веб-диалоги, где есть хотя бы одно сообщение, свежие сверху (§2.2).

    `identity_ids` — фильтр «Комната» (идентичности привязок её проживаний):
    None — без фильтра, пустой набор — пустой список. `since`/`until` — фильтр
    «Дата»: в диалоге есть сообщение в `[since, until)`.
    """
    if identity_ids is not None and not identity_ids:
        return []
    latest = (
        select(Message.conversation_id, func.max(Message.created_at).label("last_at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    query = (
        select(Conversation.id, Conversation.guest_identity_id)
        .join(latest, latest.c.conversation_id == Conversation.id)
        .where(Conversation.channel == CHANNEL)
    )
    if identity_ids is not None:
        query = query.where(Conversation.guest_identity_id.in_(list(identity_ids)))
    if since is not None and until is not None:
        query = query.where(
            select(Message.id)
            .where(
                Message.conversation_id == Conversation.id,
                Message.created_at >= since,
                Message.created_at < until,
            )
            .exists()
        )
    query = query.order_by(latest.c.last_at.desc(), Conversation.id.desc()).limit(limit)
    async with session_scope() as session:
        heads = (await session.execute(query)).all()
        if not heads:
            return []
        # Последнее сообщение каждого диалога — DISTINCT ON по диалогу.
        last_rows = await session.execute(
            select(
                Message.conversation_id,
                Message.direction,
                Message.content_kind,
                Message.text,
                Message.created_at,
            )
            .where(Message.conversation_id.in_([head.id for head in heads]))
            .order_by(Message.conversation_id, Message.created_at.desc(), Message.id.desc())
            .distinct(Message.conversation_id)
        )
        last = {
            conversation_id: _message(direction, kind, text, created_at)
            for conversation_id, direction, kind, text, created_at in last_rows
        }
    return [
        ConversationRow(
            id=head.id, guest_identity_id=head.guest_identity_id, last_message=last[head.id]
        )
        for head in heads
        if head.id in last
    ]


async def get_web_conversation(conversation_id: uuid.UUID) -> ConversationHead | None:
    """Шапка веб-диалога; None — нет такого, чужой отель или не веб-чат (404)."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    Conversation.id, Conversation.guest_identity_id, Conversation.created_at
                ).where(Conversation.id == conversation_id, Conversation.channel == CHANNEL)
            )
        ).first()
    return None if row is None else ConversationHead(*row)


async def list_conversation_messages(conversation_id: uuid.UUID, *, limit: int) -> list[MessageRow]:
    """Последние `limit` сообщений диалога в хронологическом порядке (§2.3)."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Message.direction, Message.content_kind, Message.text, Message.created_at)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
        ).all()
    messages = [_message(*row) for row in rows]
    messages.reverse()
    return messages


async def web_conversations_for_requests(
    request_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Веб-диалог, из которого родилась заявка (§2.4): заявку завёл бот
    (`request_origins`, ADR-011) или она — карточка эскалации
    (`conversation_escalations.request_id`, spec 0039 §2.5)."""
    if not request_ids:
        return {}
    ids = list(request_ids)
    async with session_scope() as session:
        origins = await session.execute(
            select(RequestOrigin.request_id, RequestOrigin.conversation_id)
            .join(Conversation, Conversation.id == RequestOrigin.conversation_id)
            .where(RequestOrigin.request_id.in_(ids), Conversation.channel == CHANNEL)
        )
        found = {request_id: conversation_id for request_id, conversation_id in origins}
        escalations = await session.execute(
            select(ConversationEscalation.request_id, ConversationEscalation.conversation_id)
            .join(Conversation, Conversation.id == ConversationEscalation.conversation_id)
            .where(ConversationEscalation.request_id.in_(ids), Conversation.channel == CHANNEL)
        )
        for request_id, conversation_id in escalations:
            if request_id is not None and conversation_id is not None:
                found.setdefault(request_id, conversation_id)
    return found
