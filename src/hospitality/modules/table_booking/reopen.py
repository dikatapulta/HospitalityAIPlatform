"""Возврат отвеченного запроса столика в работу: ответ ресторана снимается (#998).

Ответ ресторана закрывает заявку `done` (`answers.py`), и «Вернуть в работу»
(`requests.reopen_request`, spec 0080) открывает её снова. Но ответ лежит в брони,
а стадия брони идёт от ответа раньше статуса заявки (`tracking.booking_stage`): без
снятия бронь осталась бы «подтверждена», «Ответить» не вернулось бы, а повтор того
же ответа закрыл бы заявку прежним ответом (Н-1 `answers.py`).

Подписчик `request.status_changed` (`done → in_progress`, P-6): домен заявок о брони
не знает, а кнопка возврата есть и в кабинете, и в чате службы. Ответ снимается под
`FOR UPDATE` брони и только пока заявка открыта: повтор доставки события после нового
ответа и закрытия новый ответ не трогает (P-8). Поправку гостю шлёт подписчик сообщений
`channels/web/table/messages.py` — ему ответ не нужен.
"""

from __future__ import annotations

from sqlalchemy import select

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking.models import TableBooking
from hospitality.modules.table_booking.tracking import OPEN_STATUSES
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_table_booking

logger = get_logger(module=__name__)


async def withdraw_answer_on_reopen(event: requests_api.RequestStatusChanged) -> None:
    """Снять ответ ресторана с брони, чью заявку вернули в работу."""
    if not (
        event.old_status is requests_api.RequestStatus.DONE
        and event.new_status is requests_api.RequestStatus.IN_PROGRESS
    ):
        return
    request = await requests_api.get_request(event.request_id)
    async with session_scope() as session:
        row = await session.scalar(
            select(TableBooking)
            .where(TableBooking.request_id == event.request_id)
            .with_for_update()
        )
        if row is None or row.answer is None:
            return  # заявка не бронь или закрыта без ответа («ресторан не ответил»)
        if request.status not in OPEN_STATUSES:
            logger.info("table_booking_answer_kept_closed", request_id=str(event.request_id))
            return
        answer = row.answer
        row.answer = None
        row.proposed_at = None
        row.answered_at = None
        row.answered_by_user_id = None
        row.answered_by_display_name = None
    record_table_booking("answer_withdrawn")
    logger.info(
        "table_booking_answer_withdrawn",
        booking_id=str(row.id),
        request_id=str(event.request_id),
        answer=answer.value,
    )
