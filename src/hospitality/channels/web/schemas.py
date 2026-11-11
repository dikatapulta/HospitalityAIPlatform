"""Pydantic-схемы HTTP-границы канала web (spec 0027 §3, R-6, P-7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from hospitality.modules.feedback.api import FeedbackRating


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
    (страница генерирует UUID на каждое новое сообщение, а повтор недоставленного
    шлёт с прежним — дошедший первый раз не станет вторым платным ходом, #117;
    P-8, как update_id у Telegram)."""

    text: str = Field(min_length=1, max_length=4000)
    client_message_id: uuid.UUID


class SendMessageResult(BaseModel):
    """Синхронный итог хода: реплики платформы этого хода (обычно одна).

    `reply_ids` — id реплик в истории, по порядку `replies`: опрос принесёт их
    снова, и страница узнаёт уже показанное по id, а не по курсору (#117).
    `duplicate=True` — повтор `client_message_id`: ход не выполнялся, реплик нет
    (страница дозаберёт исход первого хода poll'ом). `pause` — текст отказа
    лимита, когда ход остановлен лимитом чата, а реплики нет (отказ в этом
    окне уже уходил, spec 0023 §3): страница показывает его статусом под
    строкой ввода, а не тишиной (#222).

    `last_message_id` — id последнего сообщения хода; им двигали курсор опроса
    страницы до #117, и прыжок курсора терял исходящее подписчика, записанное
    между входящим и репликой. Новая страница курсор берёт только из опроса;
    поле живёт для страниц, открытых гостями до обновления.
    """

    replies: list[str]
    reply_ids: list[uuid.UUID] = Field(default_factory=list)
    duplicate: bool = False
    last_message_id: uuid.UUID | None = None
    pause: str | None = None


class MessageFeedback(BaseModel):
    """Оценка заявки под сообщением о её выполнении (spec 0076 §5): `rating` None — кнопки.

    `passed_to_staff` — карточка по оценке есть: без неё гостю не говорят «передадим»
    и «передали» (`FeedbackRead.passed_to_staff`).
    """

    request_id: uuid.UUID
    rating: FeedbackRating | None
    commented: bool
    passed_to_staff: bool


class ChatMessage(BaseModel):
    """Сообщение истории для страницы (poll, spec 0027 §3.2).

    `feedback` — только у итога `done` заявки этого проживания у отеля с включёнными
    оценками (spec 0076 §5); у остальных сообщений — None, виджета нет.
    `client_message_id` — у входящего: ключ отправки страницы. Страница рисует
    сообщение гостя до ответа сервера и по этому ключу узнаёт его в опросе (#117).
    """

    id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    text: str
    created_at: datetime
    feedback: MessageFeedback | None = None
    client_message_id: str | None = None


class MessagesPage(BaseModel):
    """Ответ poll'а: сообщения после курсора `after` (или свежий хвост) и комната сессии
    (spec 0068 §2.2): страница сверяет её с комнатой своего адреса."""

    messages: list[ChatMessage]
    room_number: str
