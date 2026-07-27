"""Pydantic-схемы HTTP-границы канала web (spec 0027 §3, R-6, P-7)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class StartSessionBody(BaseModel):
    """Тело привязки: код заселения с карточки ресепшена (ADR-008 §3)."""

    code: str = Field(min_length=1, max_length=32)


class StartSessionResult(BaseModel):
    """Успешная привязка; токен уезжает ТОЛЬКО в HttpOnly-cookie, не в тело."""

    room_number: str
    check_out_at: datetime


class SendMessageBody(BaseModel):
    """Сообщение гостя. `client_message_id` — ключ идемпотентности повтора
    (страница генерирует UUID на каждую отправку; ретрай той же отправки не
    создаёт второго сообщения — P-8, как update_id у Telegram)."""

    text: str = Field(min_length=1, max_length=4000)
    client_message_id: uuid.UUID


class SendMessageResult(BaseModel):
    """Синхронный итог хода: реплики платформы этого хода (обычно одна).

    `duplicate=True` — повтор `client_message_id`: ход не выполнялся,
    реплик нет (страница просто дожидается poll'а).
    """

    replies: list[str]
    duplicate: bool = False


class ChatMessage(BaseModel):
    """Сообщение истории для страницы (poll, spec 0027 §3.2)."""

    id: uuid.UUID
    direction: str  # inbound | outbound
    text: str
    created_at: datetime


class MessagesPage(BaseModel):
    """Ответ poll'а: сообщения после курсора `after` (или свежий хвост)."""

    messages: list[ChatMessage]
