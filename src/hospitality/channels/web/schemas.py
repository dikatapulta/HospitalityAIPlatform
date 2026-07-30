"""Pydantic-схемы HTTP-границы канала web (spec 0027 §3, R-6, P-7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StartSessionBody(BaseModel):
    """Тело привязки: код заселения с карточки ресепшена (ADR-008 §3)."""

    code: str = Field(min_length=1, max_length=32)


class StartSessionResult(BaseModel):
    """Успешная привязка; токен уезжает ТОЛЬКО в HttpOnly-cookie, не в тело."""

    room_number: str
    check_out_at: datetime


class BindSessionResult(BaseModel):
    """Успешная привязка по QR-ссылке (spec 0033 §6): страница уходит по
    `chat_url` в обычный чат; токен — ТОЛЬКО в HttpOnly-cookie, не в тело."""

    room_number: str
    chat_url: str


class SendMessageBody(BaseModel):
    """Сообщение гостя. `client_message_id` — ключ идемпотентности повтора
    (страница генерирует UUID на каждую отправку; ретрай той же отправки не
    создаёт второго сообщения — P-8, как update_id у Telegram)."""

    text: str = Field(min_length=1, max_length=4000)
    client_message_id: uuid.UUID


class SendMessageResult(BaseModel):
    """Синхронный итог хода: реплики платформы этого хода (обычно одна).

    `last_message_id` — id последнего сообщения, записанного этим ходом
    (реплика или само входящее): страница ставит его курсором poll'а — без
    этого следующий опрос вернул бы уже показанные сообщения второй раз
    (дубли, находка живой проверки 27.07). `duplicate=True` — повтор
    `client_message_id`: ход не выполнялся, реплик нет, курсор не двигается
    (страница дозаберёт исход первого хода poll'ом).
    """

    replies: list[str]
    duplicate: bool = False
    last_message_id: uuid.UUID | None = None


class ChatMessage(BaseModel):
    """Сообщение истории для страницы (poll, spec 0027 §3.2)."""

    id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    text: str
    created_at: datetime


class MessagesPage(BaseModel):
    """Ответ poll'а: сообщения после курсора `after` (или свежий хвост)."""

    messages: list[ChatMessage]
