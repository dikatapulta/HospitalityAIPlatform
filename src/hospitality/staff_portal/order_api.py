"""JSON-действия карточки заказа в номер (spec 0038 §4, §6 PR 6; issue #418).

«Оплачено» и «Отменить» с причиной — по `request_id` заявки заказа, как у
действий очереди; «Иду за оплатой» и «Доставлено» — общие `claim` и `complete`
(`api_router.py`). Канон `api_router.py`: CSRF-щит
`browser.require_json_same_origin`, личность — `staff_auth.require_role`, службы
сотрудника уходят домену (`allowed_category_ids`, spec 0045 §5) — заявка чужой
службы — 403 `ERR-REQUESTS-007` под блокировкой строки. Флаг room service
отеля не проверяется намеренно: выключили приём — открытые заказы всё равно
надо довести или отменить. Заявка не заказ — 404 `ERR-ROOMSERVICE-008`.
Действия зовёт `static/queue.js`.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, field_validator

from hospitality.modules.room_service import api as room_service_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import scope
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/orders", tags=["staff-portal"])

# Уровень — любой, как у очереди; граница — служба заявки в домене.
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)

Reason = room_service_api.OrderCancelReason


class OrderCancelBody(BaseModel):
    """«Отменить заказ»: причина из списка сотрудника — гость прочтёт её своими словами."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason

    @field_validator("reason")
    @classmethod
    def _staff_reason(cls, value: Reason) -> Reason:
        if value is Reason.BY_GUEST:
            raise ValueError("by_guest is the guest's own cancel, not a staff reason")
        return value


async def _scoped_actor(
    request: Request,
) -> tuple[staff_auth.StaffContext, frozenset[uuid.UUID] | None]:
    require_json_same_origin(request)
    context = await _any_role(request)
    return context, await scope.load_allowed_category_ids(context)


async def _order(request_id: uuid.UUID) -> room_service_api.OrderRead:
    order = await room_service_api.get_order_by_request(request_id)
    if order is None:
        raise AppError(
            code=room_service_api.ERR_ROOMSERVICE_ORDER_NOT_FOUND,
            message=f"Service request {request_id} is not a room service order",
            status_code=404,
        )
    return order


@router.post("/{request_id}/paid", summary="Заказ оплачен — деньги у сотрудника")
async def mark_order_paid(request: Request, request_id: uuid.UUID) -> room_service_api.OrderRead:
    """Отметка «Оплачено» (Р-2): только у принятого заказа, один раз — иначе
    409 `ERR-REQUESTS-008`. Тела нет — `{}` ради Content-Type из CSRF-контракта."""
    context, allowed = await _scoped_actor(request)
    order = await _order(request_id)
    updated = await room_service_api.mark_order_paid(
        order.id, acting_user=context.actor(), allowed_category_ids=allowed
    )
    logger.info(
        "staff.order_paid",
        request_id=str(request_id),
        catalog=order.catalog.value,
        user_id=str(context.user_id),
    )
    return updated


@router.post("/{request_id}/cancel", summary="Отменить заказ с причиной")
async def cancel_order(
    request: Request, request_id: uuid.UUID, body: OrderCancelBody
) -> room_service_api.OrderRead:
    """Причина, затем отмена заявки; у оплаченного «Отказался платить» — 409
    `ERR-ROOMSERVICE-009` (ревью PR #497, Н-7)."""
    context, allowed = await _scoped_actor(request)
    order = await _order(request_id)
    updated = await room_service_api.cancel_order_by_staff(
        order.id, body.reason, acting_user=context.actor(), allowed_category_ids=allowed
    )
    logger.info(
        "staff.order_cancelled",
        request_id=str(request_id),
        catalog=order.catalog.value,
        reason=body.reason.value,
        user_id=str(context.user_id),
    )
    return updated
