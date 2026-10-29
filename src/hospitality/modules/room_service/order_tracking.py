"""Заказ после «Оформить» (spec 0038 §4, PR 5 серии): чтение, стадия, отмены, ретеншн.

Оформление — `orders.py`; здесь всё, что делается с уже записанным заказом.
Стадия выводится из заявки (Р-4): модуль хранит корзину, суммы и причину
отмены, а шаги и их время — у заявки. Заказ без заявки (`request_created`
ложно) не видит никто, кроме ретеншна. Каждая функция вызывается внутри
`tenant_context`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service.models import (
    OrderCancelReason,
    RoomServiceOrder,
    RoomServiceOrderLine,
)
from hospitality.modules.room_service.schemas import OrderLineRead, OrderRead, OrderStage
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_room_service_order

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_ROOMSERVICE_ALREADY_ACCEPTED: Final = "ERR-ROOMSERVICE-007"
ERR_ROOMSERVICE_ORDER_NOT_FOUND: Final = "ERR-ROOMSERVICE-008"
ERR_ROOMSERVICE_REASON_NOT_FOR_PAID: Final = "ERR-ROOMSERVICE-009"

# Заказ без заявки старше этого удаляет ретеншн (Р-3).
ABANDONED_AFTER: Final = timedelta(days=1)
_ACTIVE_STAGES: Final = frozenset({OrderStage.SENT, OrderStage.ACCEPTED, OrderStage.PAID})


async def get_order_for_stay(order_id: uuid.UUID, stay_id: uuid.UUID) -> OrderRead:
    """Заказ этого проживания; чужой, без заявки или несуществующий — 008."""
    async with session_scope() as session:
        order = await session.scalar(
            select(RoomServiceOrder).where(
                RoomServiceOrder.id == order_id,
                RoomServiceOrder.stay_id == stay_id,
                RoomServiceOrder.request_created,
            )
        )
        if order is None:
            raise order_not_found(f"Room service order {order_id} not found")
        lines = await lines_of(session, order.id)
    return read_order(order, lines, await requests_api.get_request(order.request_id))


async def list_active_orders_for_stay(stay_id: uuid.UUID) -> list[OrderRead]:
    """Открытые заказы проживания — «Отправлен», «Принят», «Оплачен»; новые первыми."""
    async with session_scope() as session:
        orders = (
            await session.scalars(
                select(RoomServiceOrder)
                .where(RoomServiceOrder.stay_id == stay_id, RoomServiceOrder.request_created)
                .order_by(RoomServiceOrder.created_at.desc(), RoomServiceOrder.id)
            )
        ).all()
        lines = {order.id: await lines_of(session, order.id) for order in orders}
    requests = {
        request.id: request
        for request in await requests_api.list_requests_by_ids([o.request_id for o in orders])
    }
    result = [
        read_order(order, lines[order.id], requests[order.request_id])
        for order in orders
        if order.request_id in requests
    ]
    return [order for order in result if order.stage in _ACTIVE_STAGES]


async def get_order_by_request(request_id: uuid.UUID) -> OrderRead | None:
    """Заказ заявки — подписчику сообщений гостю и карточке кабинета; None — не заказ."""
    async with session_scope() as session:
        order = await session.scalar(
            select(RoomServiceOrder).where(RoomServiceOrder.request_id == request_id)
        )
        if order is None:
            return None
        lines = await lines_of(session, order.id)
    return read_order(order, lines, await requests_api.get_request(request_id))


async def orders_for_requests(
    requests: Sequence[requests_api.ServiceRequestRead],
) -> dict[uuid.UUID, OrderRead]:
    """Заказы уже прочитанных заявок — очереди и сигналу кабинета, двумя запросами.

    Ключ — `request_id`; заявка не заказ — её в словаре нет. Заявки не
    перечитываются: очередь их уже держит, и стадия выводится из них же (Р-4).
    """
    by_id = {request.id: request for request in requests}
    if not by_id:
        return {}
    async with session_scope() as session:
        orders = (
            await session.scalars(
                select(RoomServiceOrder).where(RoomServiceOrder.request_id.in_(by_id))
            )
        ).all()
        if not orders:
            return {}
        rows = await session.scalars(
            select(RoomServiceOrderLine)
            .where(RoomServiceOrderLine.order_id.in_([order.id for order in orders]))
            .order_by(RoomServiceOrderLine.order_id, RoomServiceOrderLine.position)
        )
        lines: dict[uuid.UUID, list[OrderLineRead]] = {}
        for row in rows:
            lines.setdefault(row.order_id, []).append(OrderLineRead.model_validate(row))
    return {
        order.request_id: read_order(order, lines.get(order.id, []), by_id[order.request_id])
        for order in orders
    }


async def order_guest_identity(request_id: uuid.UUID) -> uuid.UUID | None:
    """Идентичность гостя заказа — адрес сообщений в его веб-чат (Р-8); None — нет."""
    async with session_scope() as session:
        identity: uuid.UUID | None = await session.scalar(
            select(RoomServiceOrder.guest_identity_id).where(
                RoomServiceOrder.request_id == request_id
            )
        )
    return identity


async def cancel_order_by_guest(order_id: uuid.UUID, stay_id: uuid.UUID) -> OrderRead:
    """Гость отменяет свой заказ — только «Отправлен»; принятый — 007.

    Причина пишется до перехода (подписчик читает её по событию), переход —
    с `only_from=new` под блокировкой заявки: «гость отменяет» и «сотрудник
    берёт» выигрывает один. Проиграл — причина снимается, если её не сменил
    сотрудник, отменивший в ту же секунду (ревью PR #520, Н-6).
    """
    order = await get_order_for_stay(order_id, stay_id)
    if order.stage is OrderStage.CANCELLED:
        return order
    if order.stage is not OrderStage.SENT:
        raise already_accepted(order_id)
    previous = await swap_cancel_reason(order.id, OrderCancelReason.BY_GUEST)
    try:
        await requests_api.change_request_status(
            order.request_id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.GUEST,
            only_from=requests_api.RequestStatus.NEW,
        )
    except AppError as error:
        await restore_cancel_reason(order.id, previous, written=OrderCancelReason.BY_GUEST)
        if error.code in {
            requests_api.ERR_REQUESTS_INVALID_STATUS_TRANSITION,
            requests_api.ERR_REQUESTS_PAYMENT_NOT_APPLICABLE,
        }:
            raise already_accepted(order_id) from error
        raise
    record_room_service_order(order.catalog.value, "cancelled_by_guest")
    logger.info("room_service_order_cancelled_by_guest", order_id=str(order_id))
    return await get_order_for_stay(order_id, stay_id)


async def cancel_order_by_staff(
    order_id: uuid.UUID,
    reason: OrderCancelReason,
    *,
    acting_user: requests_api.ActingUser,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> OrderRead:
    """Сотрудник отменяет заказ с причиной: причина, затем отмена заявки (§4).

    Граница службы — у перехода заявки, под её блокировкой (`allowed_category_ids`,
    канон spec 0045 §5; ревью PR #497 Н-4). Отказ перехода возвращает прежнюю причину.
    «Отказался платить» у оплаченного — 009: отметку не снять, и гость прочёл бы
    «оплата не состоялась» рядом с «деньги вернут» (ревью PR #497, Н-7).
    """
    if reason is OrderCancelReason.BY_GUEST:
        raise ValueError("staff cancel needs a staff reason")
    order = await _staff_order(order_id)
    if reason is OrderCancelReason.PAYMENT_REFUSED:
        request = await requests_api.get_request(order.request_id)
        own = allowed_category_ids is None or request.category_id in allowed_category_ids
        # Чужой службе отказ о стадии не рассказывает — её остановит переход (007).
        if own and request.paid_at is not None:
            raise AppError(
                code=ERR_ROOMSERVICE_REASON_NOT_FOR_PAID,
                message="A paid order cannot be cancelled as payment refused",
                status_code=409,
            )
    previous = await swap_cancel_reason(order.id, reason)
    try:
        await requests_api.change_request_status(
            order.request_id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.STAFF,
            acting_user=acting_user,
            allowed_category_ids=allowed_category_ids,
        )
    except AppError:
        await restore_cancel_reason(order.id, previous, written=reason)
        raise
    record_room_service_order(order.catalog.value, "cancelled_by_staff")
    logger.info(
        "room_service_order_cancelled_by_staff", order_id=str(order_id), reason=reason.value
    )
    return await _read_by_request(order.request_id)


async def mark_order_paid(
    order_id: uuid.UUID,
    *,
    acting_user: requests_api.ActingUser,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> OrderRead:
    """«Оплачено» из кабинета: отметка на заявке заказа (Р-2) — граница службы и
    отказы (007, ERR-REQUESTS-008) у `requests_api.mark_request_paid`."""
    order = await _staff_order(order_id)
    await requests_api.mark_request_paid(
        order.request_id, acting_user=acting_user, allowed_category_ids=allowed_category_ids
    )
    record_room_service_order(order.catalog.value, "paid")
    logger.info("room_service_order_paid", order_id=str(order_id))
    return await _read_by_request(order.request_id)


async def _staff_order(order_id: uuid.UUID) -> RoomServiceOrder:
    """Заказ для шага сотрудника: любой с заявкой, чужого тенанта не видно (RLS)."""
    async with session_scope() as session:
        order = await session.scalar(
            select(RoomServiceOrder).where(RoomServiceOrder.id == order_id)
        )
    if order is None or not order.request_created:
        raise order_not_found(f"Room service order {order_id} not found")
    return order


async def _read_by_request(request_id: uuid.UUID) -> OrderRead:
    read = await get_order_by_request(request_id)
    assert read is not None  # строка заказа не удаляется, пока у неё есть заявка
    return read


def order_stage(request: requests_api.ServiceRequestRead) -> OrderStage:
    """Стадия заказа из заявки (Р-4): отмена и выполнение главнее отметки оплаты."""
    if request.status is requests_api.RequestStatus.CANCELLED:
        return OrderStage.CANCELLED
    if request.status is requests_api.RequestStatus.DONE:
        return OrderStage.DELIVERED
    if request.paid_at is not None:
        return OrderStage.PAID
    if request.status is requests_api.RequestStatus.IN_PROGRESS:
        return OrderStage.ACCEPTED
    return OrderStage.SENT


async def delete_abandoned_orders(*, created_before: datetime) -> int:
    """Удалить заказы без заявки старше границы — ретеншн, шаг брошенных (Р-3)."""
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(RoomServiceOrder).where(
                    RoomServiceOrder.created_at < created_before,
                    RoomServiceOrder.request_created.is_(False),
                )
            ),
        )
    return result.rowcount or 0


async def detach_expired_orders(*, created_before: datetime) -> int:
    """Обезличить заказы старше границы: связь с проживанием и гостем — NULL.

    Срок — общий с текстом заявки (spec 0032; ревью PR #497 Н-2): состав и суммы
    остаются отчётам отеля, кто именно заказывал, через заказ уже не узнать.
    """
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(RoomServiceOrder)
                .where(
                    RoomServiceOrder.created_at < created_before,
                    (RoomServiceOrder.stay_id.is_not(None))
                    | (RoomServiceOrder.guest_identity_id.is_not(None)),
                )
                .values(stay_id=None, guest_identity_id=None)
            ),
        )
    return result.rowcount or 0


async def lines_of(session: AsyncSession, order_id: uuid.UUID) -> list[OrderLineRead]:
    rows = await session.scalars(
        select(RoomServiceOrderLine)
        .where(RoomServiceOrderLine.order_id == order_id)
        .order_by(RoomServiceOrderLine.position)
    )
    return [OrderLineRead.model_validate(row) for row in rows]


async def swap_cancel_reason(
    order_id: uuid.UUID, reason: OrderCancelReason | None
) -> OrderCancelReason | None:
    """Записать причину отмены и вернуть прежнюю — откат при проигранном переходе."""
    async with session_scope() as session:
        order = await session.scalar(
            select(RoomServiceOrder).where(RoomServiceOrder.id == order_id).with_for_update()
        )
        if order is None:  # pragma: no cover — строку заказа держит его заявка
            raise order_not_found(f"Room service order {order_id} not found")
        previous = order.cancel_reason
        order.cancel_reason = reason
    return previous


async def restore_cancel_reason(
    order_id: uuid.UUID,
    previous: OrderCancelReason | None,
    *,
    written: OrderCancelReason,
) -> None:
    """Откат причины проигравшего перехода — только если в строке всё ещё своя.

    Гость и сотрудник отменяют в одну секунду: победитель уже записал свою
    причину, и слепой откат проигравшего затёр бы её — гость прочёл бы
    «Другое» (ревью PR #520, Н-6).
    """
    async with session_scope() as session:
        await session.execute(
            update(RoomServiceOrder)
            .where(RoomServiceOrder.id == order_id, RoomServiceOrder.cancel_reason == written)
            .values(cancel_reason=previous)
        )


def read_order(
    order: RoomServiceOrder,
    lines: list[OrderLineRead],
    request: requests_api.ServiceRequestRead,
) -> OrderRead:
    return OrderRead(
        id=order.id,
        catalog=order.catalog,
        request_id=order.request_id,
        number=request.daily_number,
        stage=order_stage(request),
        room_number=request.room_number,
        guest_language=order.guest_language,
        currency=order.currency,
        lines=lines,
        subtotal_minor=order.subtotal_minor,
        service_charge_percent=order.service_charge_percent,
        service_charge_minor=order.service_charge_minor,
        total_minor=order.total_minor,
        comment=request.details,
        cancel_reason=order.cancel_reason,
        created_at=order.created_at,
        accepted_at=request.claimed_at,
        paid_at=request.paid_at,
        closed_at=request.closed_at,
    )


def order_not_found(message: str) -> AppError:
    return AppError(code=ERR_ROOMSERVICE_ORDER_NOT_FOUND, message=message, status_code=404)


def already_accepted(order_id: uuid.UUID) -> AppError:
    return AppError(
        code=ERR_ROOMSERVICE_ALREADY_ACCEPTED,
        message=f"Room service order {order_id} is already accepted",
        status_code=409,
    )
