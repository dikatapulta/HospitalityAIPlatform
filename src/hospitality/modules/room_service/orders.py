"""Заказы в номер (spec 0038 §4, PR 5 серии) — копия канона `modules/requests/service.py`.

Заказ — корзина и суммы модуля плюс заявка в службе каталога (Р-1): номер `#N`,
комната, пожелания, шаги и их время живут в заявке, стадия выводится из неё
(`order_stage`, Р-4). Запись — порядком Р-3: строка заказа с `request_id =
uuid4()` → `requests.create_request(request_id=…)`, идемпотентный по id →
отметка `request_created`. Сбой между шагами — заказ без заявки: его не видит
никто, повтор «Оформить» тем же ключом доводит дело, брошенный удаляет ретеншн
через сутки (`order_tracking.delete_abandoned_orders`). Что делается с заказом
после «Оформить» — чтение, стадия, отмены, — `order_tracking.py`.

Каждая функция вызывается внутри `tenant_context`. Чья это сессия гостя и какое
проживание — решает канал; домен перепроверяет, что проживание активно.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, time, tzinfo
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service.models import (
    RoomServiceCatalog,
    RoomServiceItem,
    RoomServiceOrder,
    RoomServiceOrderLine,
    RoomServiceSection,
    RoomServiceSettings,
)
from hospitality.modules.room_service.order_tracking import lines_of, order_not_found, read_order
from hospitality.modules.room_service.schemas import (
    MAX_OPEN_ORDERS_PER_STAY,
    MAX_ORDERS_PER_HOTEL_DAY,
    CartLine,
    OrderLineRead,
    OrderPlace,
    OrderPlaced,
    OrderRead,
    Quote,
    QuoteRequest,
    format_amount,
    service_charge_minor,
)
from hospitality.modules.room_service.service import (
    ERR_ROOMSERVICE_CATALOG_DISABLED,
    ERR_ROOMSERVICE_NOT_FOUND,
    open_catalog_with_config,
)
from hospitality.platform.config import LocalizedText, TenantConfig
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_room_service_order
from hospitality.shared.pii import mask_payment_card_numbers
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_ROOMSERVICE_NOT_ACCEPTING: Final = "ERR-ROOMSERVICE-003"
ERR_ROOMSERVICE_ITEM_UNAVAILABLE: Final = "ERR-ROOMSERVICE-004"
ERR_ROOMSERVICE_TOTAL_CHANGED: Final = "ERR-ROOMSERVICE-005"
ERR_ROOMSERVICE_ORDER_LIMIT: Final = "ERR-ROOMSERVICE-006"

_CLIENT_KEY_CONSTRAINT: Final = "uq_room_service_orders_client_key"
_SUMMARY_LIMIT: Final = 500


async def quote(data: QuoteRequest) -> Quote:
    """Итог корзины по ценам сервера: сумма, сбор каталога, итог (Р-5).

    Не принимает ли каталог заказы — не проверяет: итог гость видит и в меню.
    Товар чужого каталога или несуществующий — 001; скрытый или «Закончилось» — 004.
    """
    async with session_scope() as session:
        settings, config = await open_catalog_with_config(session, data.catalog)
        lines = await _price_lines(session, data.catalog, data.lines)
    return _quote(data.catalog, _currency(config), lines, settings.service_charge_percent)


async def place_order(data: OrderPlace) -> OrderPlaced:
    """Оформить заказ: проверки, строка заказа, заявка в службу каталога.

    Повтор тем же `client_order_key` у того же проживания — тот же заказ
    (`duplicate=True`), и заявка доводится, если в прошлый раз не успела.
    Порядок отказов: каталог закрыт (002), не принимает (003), товар (001/004),
    итог разошёлся (005), лимит проживания (006).
    """
    catalog = data.catalog.value
    existing = await _find_by_client_key(data.stay_id, data.client_order_key)
    if existing is not None:
        record_room_service_order(catalog, "duplicate")
        return OrderPlaced(order=await _ensure_request(existing, data.comment), duplicate=True)
    stay = await guests_api.get_active_stay(data.stay_id)
    if stay is None:
        raise order_not_found(f"Stay {data.stay_id} is not active")
    async with session_scope() as session:
        settings, config = await open_catalog_with_config(session, data.catalog)
        accepting = await session.get(RoomServiceSettings, (current_tenant_id(), data.catalog))
        if accepting is None or not accepting.accepting_orders:
            record_room_service_order(catalog, "not_accepting")
            raise AppError(
                code=ERR_ROOMSERVICE_NOT_ACCEPTING,
                message=f"Room service catalog {catalog!r} is not accepting orders",
                status_code=409,
            )
        try:
            lines = await _price_lines(session, data.catalog, data.lines)
        except AppError as error:
            if error.code == ERR_ROOMSERVICE_ITEM_UNAVAILABLE:
                record_room_service_order(catalog, "unavailable")
            raise
        stay_orders = (
            await session.execute(
                select(RoomServiceOrder.request_id, RoomServiceOrder.created_at).where(
                    RoomServiceOrder.stay_id == stay.id, RoomServiceOrder.request_created
                )
            )
        ).all()
    priced = _quote(data.catalog, _currency(config), lines, settings.service_charge_percent)
    if priced.total_minor != data.expected_total_minor:
        record_room_service_order(catalog, "total_changed")
        raise AppError(
            code=ERR_ROOMSERVICE_TOTAL_CHANGED,
            message=f"Order total changed: now {priced.total_minor}",
            status_code=409,
        )
    await _check_limits(catalog, stay_orders, _hotel_day_start(config.tzinfo))
    category_id = await _category_id(settings.category_key)

    order = RoomServiceOrder(
        id=uuid.uuid4(),
        tenant_id=current_tenant_id(),
        catalog=data.catalog,
        request_id=uuid.uuid4(),
        request_created=False,
        stay_id=stay.id,
        guest_identity_id=data.guest_identity_id,
        client_order_key=data.client_order_key,
        guest_language=data.guest_language,
        currency=priced.currency,
        subtotal_minor=priced.subtotal_minor,
        service_charge_percent=priced.service_charge_percent,
        service_charge_minor=priced.service_charge_minor,
        total_minor=priced.total_minor,
    )
    try:
        async with session_scope() as session:
            session.add(order)
            await session.flush()
            for position, line in enumerate(priced.lines):
                session.add(
                    RoomServiceOrderLine(
                        tenant_id=order.tenant_id,
                        order_id=order.id,
                        item_id=line.item_id,
                        position=position,
                        names=line.names.as_json(),
                        unit_price_minor=line.unit_price_minor,
                        quantity=line.quantity,
                        line_total_minor=line.line_total_minor,
                    )
                )
    except IntegrityError as error:
        # Двойное «Оформить» в одну секунду: второй проигрывает ключу и отдаёт заказ первого.
        if _CLIENT_KEY_CONSTRAINT not in str(error):
            raise
        winner = await _find_by_client_key(data.stay_id, data.client_order_key)
        if winner is None:  # pragma: no cover — нарушение ключа без строки невозможно
            raise
        record_room_service_order(catalog, "duplicate")
        return OrderPlaced(order=await _ensure_request(winner, data.comment), duplicate=True)
    placed = await _ensure_request(order, data.comment, category_id=category_id)
    record_room_service_order(catalog, "created")
    logger.info(
        "room_service_order_placed",
        order_id=str(order.id),
        request_id=str(order.request_id),
        catalog=catalog,
        lines=len(priced.lines),
        total_minor=priced.total_minor,
    )
    return OrderPlaced(order=placed, duplicate=False)


async def _price_lines(
    session: AsyncSession, catalog: RoomServiceCatalog, cart: Sequence[CartLine]
) -> list[OrderLineRead]:
    rows = (
        await session.execute(
            select(RoomServiceItem, RoomServiceSection.catalog)
            .join(RoomServiceSection, RoomServiceItem.section_id == RoomServiceSection.id)
            .where(RoomServiceItem.id.in_([line.item_id for line in cart]))
        )
    ).all()
    found = {item.id: (item, item_catalog) for item, item_catalog in rows}
    priced: list[OrderLineRead] = []
    for line in cart:
        item, item_catalog = found.get(line.item_id, (None, None))
        if item is None or item_catalog is not catalog:
            raise AppError(
                code=ERR_ROOMSERVICE_NOT_FOUND,
                message=f"Room service item {line.item_id} not found in {catalog.value!r}",
                status_code=404,
            )
        if item.hidden_at is not None or not item.is_available:
            raise AppError(
                code=ERR_ROOMSERVICE_ITEM_UNAVAILABLE,
                message=f"Room service item {line.item_id} is not available",
                status_code=409,
            )
        priced.append(
            OrderLineRead(
                item_id=item.id,
                names=LocalizedText.model_validate(item.names),
                unit_price_minor=item.price_minor,
                quantity=line.quantity,
                line_total_minor=item.price_minor * line.quantity,
            )
        )
    return priced


def _quote(
    catalog: RoomServiceCatalog, currency: str, lines: list[OrderLineRead], percent: Decimal
) -> Quote:
    subtotal = sum(line.line_total_minor for line in lines)
    charge = service_charge_minor(subtotal, percent)
    return Quote(
        catalog=catalog,
        currency=currency,
        lines=lines,
        subtotal_minor=subtotal,
        service_charge_percent=percent,
        service_charge_minor=charge,
        total_minor=subtotal + charge,
    )


async def _check_limits(catalog: str, stay_orders: Sequence[Any], day_start: datetime) -> None:
    """Лимиты проживания (§4): открытых ≤ 3, за сутки отеля ≤ 10 — иначе 006."""
    today = sum(1 for row in stay_orders if row.created_at >= day_start)
    open_requests = await requests_api.list_open_requests_by_ids(
        [row.request_id for row in stay_orders]
    )
    if today >= MAX_ORDERS_PER_HOTEL_DAY or len(open_requests) >= MAX_OPEN_ORDERS_PER_STAY:
        record_room_service_order(catalog, "limited")
        logger.warning(
            "room_service_order_limited", open_orders=len(open_requests), today_orders=today
        )
        raise AppError(
            code=ERR_ROOMSERVICE_ORDER_LIMIT,
            message="Too many room service orders for this stay",
            status_code=429,
        )


def _currency(config: TenantConfig) -> str:
    return config.room_service.currency if config.room_service is not None else "KZT"


def _hotel_day_start(zone: tzinfo) -> datetime:
    """Начало суток отеля «сегодня» в UTC — граница суточного лимита (Н-10)."""
    local_today = utc_now().astimezone(zone).date()
    return datetime.combine(local_today, time.min, tzinfo=zone)


async def _category_id(category_key: str) -> uuid.UUID:
    for category in await requests_api.list_categories():
        if category.key == category_key:
            return category.id
    # Онбординг сверяет службу каталога с категориями отеля; без неё заказ некуда отдать.
    raise AppError(
        code=ERR_ROOMSERVICE_CATALOG_DISABLED,
        message=f"Room service category {category_key!r} does not exist",
        status_code=404,
    )


async def _find_by_client_key(stay_id: uuid.UUID, client_order_key: str) -> RoomServiceOrder | None:
    async with session_scope() as session:
        found: RoomServiceOrder | None = await session.scalar(
            select(RoomServiceOrder).where(
                RoomServiceOrder.stay_id == stay_id,
                RoomServiceOrder.client_order_key == client_order_key,
            )
        )
    return found


async def _ensure_request(
    order: RoomServiceOrder, comment: str | None, *, category_id: uuid.UUID | None = None
) -> OrderRead:
    """Заявка заказа (идемпотентно по `request_id`) и отметка «заявка есть»."""
    async with session_scope() as session:
        lines = await lines_of(session, order.id)
    if not order.request_created:
        if category_id is None:
            async with session_scope() as session:
                settings, _ = await open_catalog_with_config(session, order.catalog)
            category_id = await _category_id(settings.category_key)
        stay = await guests_api.get_active_stay(order.stay_id) if order.stay_id else None
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_FORM,
                summary=_request_summary(lines, order.total_minor),
                details=mask_payment_card_numbers(comment) if comment else None,
                room_number=stay.room_number if stay is not None else None,
                guest_language=order.guest_language,
                payment_required=True,
            ),
            request_id=order.request_id,
            # Stay сессии, оформившей заказ (spec 0054), — и на повторе после
            # выезда, когда комнаты из активного Stay уже нет.
            stay_id=order.stay_id,
        )
        async with session_scope() as session:
            await session.execute(
                update(RoomServiceOrder)
                .where(RoomServiceOrder.id == order.id)
                .values(request_created=True)
            )
    return read_order(order, lines, await requests_api.get_request(order.request_id))


def _request_summary(lines: Sequence[OrderLineRead], total_minor: int) -> str:
    """Суть заявки для службы: состав по-русски и итог (текст заявки — 90 дней, spec 0032)."""
    items = ", ".join(f"{line.names.text_for('ru')} ×{line.quantity}" for line in lines)
    summary = f"Заказ в номер: {items} · {format_amount(total_minor, 'ru')}"
    return summary if len(summary) <= _SUMMARY_LIMIT else summary[: _SUMMARY_LIMIT - 1] + "…"
