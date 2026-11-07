"""JSON-действия карточки запроса столика (spec 0064 §4, §6 PR 3; issue #702).

Ответ ресторана и закрытие без ответа — по `request_id` заявки брони, как у
действий очереди. Канон `order_api.py`: CSRF-щит `browser.require_json_same_origin`
(JSON без `Origin` — `ERR-AUTH-009`), личность — `staff_auth.require_role`, службы
сотрудника уходят домену (`allowed_category_ids`, spec 0045 §5) — заявка чужой
службы — 403 `ERR-REQUESTS-007`. Флаг брони отеля не проверяется намеренно
(ревью PR #711, Н-3): выключили бронь — открытые запросы всё равно надо закрыть.
Заявка не бронь — 404 `ERR-TABLEBOOKING-004`. Действия зовёт `static/queue.js`.
"""

from __future__ import annotations

import uuid
from datetime import time
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import scope
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/bookings", tags=["staff-portal"])

# Уровень — любой, как у очереди; граница — служба заявки в домене.
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)


class BookingAnswerBody(BaseModel):
    """Ответ ресторана: `{"answer": "other_time", "proposed_time": "20:30"}` (§4)."""

    model_config = ConfigDict(extra="forbid")

    answer: table_booking_api.BookingAnswer
    # Только у «Другого времени»: час того же дня по часам отеля (иначе 422 — 002).
    proposed_time: time | None = None


async def _scoped_actor(
    request: Request,
) -> tuple[staff_auth.StaffContext, frozenset[uuid.UUID] | None]:
    require_json_same_origin(request)
    context = await _any_role(request)
    return context, await scope.load_allowed_category_ids(context)


@router.post("/{request_id}/answer", summary="Ответ ресторана на запрос столика")
async def answer_booking(
    request: Request, request_id: uuid.UUID, body: BookingAnswerBody
) -> table_booking_api.BookingRead:
    """Подтвердить, предложить другое время или «мест нет»: ответ закрывает заявку.

    Повтор того же ответа — без изменений; иной ответ, отменённый гостем запрос
    или час брони прошёл — 409 `ERR-TABLEBOOKING-005`; «Другое время» без часа
    или вне часов ресторана — 422 `ERR-TABLEBOOKING-002`.
    """
    context, allowed = await _scoped_actor(request)
    booking = await table_booking_api.answer_booking(
        request_id,
        body.answer,
        body.proposed_time,
        acting_user=context.actor(),
        allowed_category_ids=allowed,
    )
    logger.info(
        "staff.table_booking_answered",
        request_id=str(request_id),
        answer=body.answer.value,
        user_id=str(context.user_id),
    )
    return booking


@router.post("/{request_id}/close", summary="Закрыть запрос столика без ответа")
async def close_unanswered(
    request: Request, request_id: uuid.UUID
) -> table_booking_api.BookingRead:
    """Час брони прошёл, ответа нет — закрыть; гость получит «ресторан не ответил».

    Иначе — 409 `ERR-TABLEBOOKING-005`. Тела нет — `{}` ради Content-Type CSRF-контракта.
    """
    context, allowed = await _scoped_actor(request)
    booking = await table_booking_api.close_unanswered(
        request_id, acting_user=context.actor(), allowed_category_ids=allowed
    )
    logger.info(
        "staff.table_booking_closed_unanswered",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return booking
