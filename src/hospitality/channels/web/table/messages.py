"""Сообщения гостю в веб-чат о шагах брони столика (spec 0064 §4, §5) — канон `menu/messages.py`.

Подписчик событий заявки: `request.created` — «Запрос отправлен»; `request.status_changed`
в `done` — ответ ресторана из брони («Подтвердить», «Другое время», «Мест нет»), а без
ответа — «не ответил»; в `cancelled` не гостем — «не ответил» (заявку закрыли общим
путём, §2.3); `done → in_progress` — поправка «ответ отправлен по ошибке» (возврат в
работу, #998: сам ответ снимает `table_booking.withdraw_answer_on_reopen`). Отмена
гостем в чат не пишется: он видит её на странице. Заявка не
бронь (и заявка «Гость отменил бронь») — подписчик молчит; общий уведомитель гостя
бронь не трогает, потому что её нет в `request_origins`.

Адрес — веб-диалог гостя по `guest_identity_id` брони. Текст — статический, на
языке брони, без модели; дата и время — по часам отеля. Ключ идемпотентности — на
шаг (`guest:table_booking:<request_id>:<шаг>`): повтор доставки события второго
сообщения не даёт (P-8). Закрытие после возврата — с номером возврата в ключе,
поправка — только к ушедшему ответу (`channels/web/form_reopen.py`).
"""

from __future__ import annotations

import enum
import uuid

import structlog

from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.web import form_reopen
from hospitality.channels.web.service import CHANNEL
from hospitality.channels.web.table.texts import MESSAGES, day_text, pick
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.shared.events import subscribe
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class BookingStep(enum.StrEnum):
    """Шаг брони, о котором пишут гостю, — и часть ключа идемпотентности."""

    REQUESTED = "requested"
    CLOSED = "closed"
    # Возврат в работу (#998): ответ ресторана ушёл по ошибке.
    REOPENED = "reopened"


def register() -> None:
    """Подключить подписчика к событиям заявки — зовёт `hospitality/worker.py`."""
    subscribe(requests_api.RequestCreated, on_request_created)
    subscribe(requests_api.RequestStatusChanged, on_request_status_changed)


async def on_request_created(event: requests_api.RequestCreated) -> None:
    await notify_booking_step(event.request_id, BookingStep.REQUESTED)


async def on_request_status_changed(event: requests_api.RequestStatusChanged) -> None:
    status = requests_api.RequestStatus
    if form_reopen.is_reopen(event):
        await notify_booking_step(event.request_id, BookingStep.REOPENED)
    elif event.new_status is status.DONE:
        await notify_booking_step(event.request_id, BookingStep.CLOSED, closed_to=status.DONE)
    elif event.new_status is status.CANCELLED:
        if event.initiator is requests_api.RequestInitiator.GUEST:
            return  # свою отмену гость видит на странице (§2.4)
        await notify_booking_step(event.request_id, BookingStep.CLOSED, closed_to=status.CANCELLED)


async def notify_booking_step(
    request_id: uuid.UUID,
    step: BookingStep,
    *,
    closed_to: requests_api.RequestStatus | None = None,
) -> None:
    """Записать гостю сообщение о шаге брони — один раз на шаг; `closed_to` — статус
    закрытия шага `CLOSED`."""
    booking = await table_booking_api.get_booking_by_request(request_id)
    if booking is None:
        return  # заявка не бронь
    identity = await table_booking_api.booking_guest_identity(request_id)
    if identity is None:
        logger.info("table_booking_message_skipped_no_guest", request_id=str(request_id))
        return
    key = await _step_key(request_id, step, closed_to)
    if key is None:
        return
    if await notification_already_sent(key):
        logger.info("table_booking_message_skipped_duplicate", request_id=str(request_id))
        return
    conversation_id = await ensure_conversation(CHANNEL, str(identity), guest_identity_id=identity)
    await record_outbound_message(
        conversation_id,
        compose_booking_message(booking, step),
        _correlation_id(),
        external_message_id=None,
        idempotency_key=key,
    )
    logger.info(
        "table_booking_message_recorded",
        request_id=str(request_id),
        step=step.value,
        answer=booking.answer.value if booking.answer else None,
        guest_language=booking.guest_language,
    )


async def _step_key(
    request_id: uuid.UUID, step: BookingStep, closed_to: requests_api.RequestStatus | None
) -> str | None:
    """Ключ шага; None — шаг не нужен гостю (возврат в работу, #998).

    Закрытие опоздало — запрос уже вернули в работу; поправка — а ответ отменённого
    закрытия гостю не уходил: поправлять нечего.
    """
    key = f"guest:table_booking:{request_id}:{step.value}"
    if step is BookingStep.REQUESTED:
        return key
    request = await requests_api.get_request(request_id)
    closed_key = f"guest:table_booking:{request_id}:{BookingStep.CLOSED.value}"
    if step is BookingStep.CLOSED:
        if closed_to is not None and form_reopen.closing_is_late(request, closed_to):
            logger.info("table_booking_message_skipped_reopened", request_id=str(request_id))
            return None
        return form_reopen.numbered_key(key, request.reopen_count)
    if not await form_reopen.closing_was_told(request, closed_key):
        logger.info("table_booking_message_skipped_not_answered", request_id=str(request_id))
        return None
    return form_reopen.reopen_key(key, request)


def compose_booking_message(booking: table_booking_api.BookingRead, step: BookingStep) -> str:
    """Текст шага брони на языке брони (§5)."""
    language = booking.guest_language
    if step is BookingStep.REQUESTED or step is BookingStep.REOPENED:
        key = step.value
    else:
        key = booking.answer.value if booking.answer is not None else "no_answer"
    return pick(MESSAGES[key], language).format(
        restaurant=booking.restaurant_names.text_for(language),
        date=day_text(booking.local_date, language),
        time=booking.local_time,
        new_time=booking.proposed_time or "",
        n=booking.party_size,
    )


def _correlation_id() -> str:
    """correlation_id события — его восстановил доставщик outbox (§10.2)."""
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else ""
