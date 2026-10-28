"""Заказы в номер (spec 0038 §3–§4, §6 PR 5): итог и сбор, отказы 003–008,
двойное «Оформить» и сбой с повтором, лимиты, отмены, стадия из заявки,
изоляция тенантов, ретеншн заказов."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.modules.room_service.api import (
    CartLine,
    ItemWrite,
    OrderCancelReason,
    OrderPlace,
    OrderStage,
    QuoteRequest,
    RoomServiceCatalog,
    SectionWrite,
)
from hospitality.modules.room_service.models import RoomServiceOrder, RoomServiceOrderLine
from hospitality.modules.room_service.schemas import service_charge_minor
from hospitality.platform.config import LocalizedText
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

RESTAURANT = RoomServiceCatalog.RESTAURANT
SHOP = RoomServiceCatalog.SHOP


@dataclass(frozen=True)
class Stand:
    tenant_id: uuid.UUID
    other_tenant_id: uuid.UUID
    stay_id: uuid.UUID
    neighbour_stay_id: uuid.UUID
    lagman: uuid.UUID
    tea: uuid.UUID
    charger: uuid.UUID
    fnb_id: uuid.UUID


async def _item(section_id: uuid.UUID, en: str, ru: str, tenge: int) -> uuid.UUID:
    names = LocalizedText(en=en, ru=ru)
    data = ItemWrite(section_id=section_id, names=names, price_minor=tenge * 100)
    return (await room_service_api.create_item(data)).id


@pytest.fixture
async def stand(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> Stand:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        fnb = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="fnb", name="F&B")
        )
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="supply", name="Снабжение")
        )
        stays = [
            await check_in(
                StayCheckIn(
                    room_number=room,
                    check_out_at=utc_now() + timedelta(days=2),
                    guest_display_name="Wang Li",
                )
            )
            for room in ("512", "514")
        ]
        mains = await room_service_api.create_section(
            RESTAURANT, SectionWrite(names=LocalizedText(en="Mains", ru="Горячее"))
        )
        lagman = await _item(mains.id, "Lagman", "Лагман", 3400)
        tea = await _item(mains.id, "Tea", "Чай", 900)
        gadgets = await room_service_api.create_section(
            SHOP, SectionWrite(names=LocalizedText(en="Gadgets"))
        )
        charger = await _item(gadgets.id, "Charger", "Зарядка", 4500)
        await room_service_api.set_accepting(RESTAURANT, True)
        await room_service_api.set_accepting(SHOP, True)
    return Stand(
        tenant_a, tenant_b, stays[0].stay.id, stays[1].stay.id, lagman, tea, charger, fnb.id
    )


def _lines(*pairs: tuple[uuid.UUID, int]) -> list[CartLine]:
    return [CartLine(item_id=item_id, quantity=quantity) for item_id, quantity in pairs]


async def _place(
    stand: Stand,
    lines: list[CartLine],
    *,
    catalog: RoomServiceCatalog = RESTAURANT,
    key: str | None = None,
    total: int | None = None,
    stay_id: uuid.UUID | None = None,
    comment: str | None = None,
) -> room_service_api.OrderPlaced:
    if total is None:
        total = (
            await room_service_api.quote(QuoteRequest(catalog=catalog, lines=lines))
        ).total_minor
    return await room_service_api.place_order(
        OrderPlace(
            catalog=catalog,
            stay_id=stay_id or stand.stay_id,
            guest_identity_id=uuid.uuid4(),
            client_order_key=key or uuid.uuid4().hex,
            guest_language="ru",
            lines=lines,
            comment=comment,
            expected_total_minor=total,
        )
    )


async def _code(awaitable: object) -> str:
    with pytest.raises(AppError) as error:
        await awaitable  # type: ignore[misc]
    return error.value.code


async def _waiter() -> requests_api.ActingUser:
    async with platform_session_scope() as session:
        user = User(display_name="Айгерим")
        session.add(user)
        await session.flush()
        return requests_api.ActingUser(user_id=user.id, display_name="Айгерим")


def test_service_charge_rounds_to_whole_tenge_half_up() -> None:
    assert service_charge_minor(5_200_00, Decimal("10")) == 520_00
    assert service_charge_minor(1_005_00, Decimal("10")) == 101_00  # 100,5 → 101
    assert service_charge_minor(1_004_00, Decimal("10")) == 100_00  # 100,4 → 100
    assert room_service_api.format_amount(5_720_00, "ru") == "5 720 ₸"
    assert room_service_api.format_amount(5_720_00, "en") == "5,720 ₸"
    assert room_service_api.format_percent(Decimal("1E+1")) == "10"
    assert room_service_api.format_percent(Decimal("12.50")) == "12.5"


async def test_order_becomes_paid_request_of_catalog_service(stand: Stand) -> None:
    """5 200 ₸ + 10% = 5 720 ₸; заказ — заявка `fnb` с оплатой при выполнении,
    с формы гостя, комната из проживания, пожелания — без номера карты."""
    with tenant_context(stand.tenant_id):
        placed = await _place(
            stand,
            _lines((stand.lagman, 1), (stand.tea, 2)),
            comment="без лука, карта 4111 1111 1111 1111",
        )
        request = await requests_api.get_request(placed.order.request_id)
        async with session_scope() as session:
            origins = await session.scalar(
                text("SELECT count(*) FROM request_origins WHERE request_id = :id"),
                {"id": request.id},
            )

    order = placed.order
    assert not placed.duplicate
    assert (order.subtotal_minor, order.service_charge_minor, order.total_minor) == (
        5_200_00,
        520_00,
        5_720_00,
    )
    assert order.stage is OrderStage.SENT
    assert order.number == request.daily_number
    assert [line.quantity for line in order.lines] == [1, 2]
    assert request.category_id == stand.fnb_id
    assert request.origin is requests_api.ServiceRequestOrigin.GUEST_FORM
    assert request.payment_required
    assert request.room_number == "512"
    assert request.summary == "Заказ в номер: Лагман ×1, Чай ×2 · 5 720 ₸"
    assert request.details is not None and "4111 1111 1111 1111" not in request.details
    assert origins == 0  # Р-8: снапшот «мои заявки» и общий уведомитель заказ не видят


async def test_repeat_key_returns_same_order_and_one_request(stand: Stand) -> None:
    with tenant_context(stand.tenant_id):
        first = await _place(stand, _lines((stand.lagman, 1)), key="double-click-1")
        second = await _place(stand, _lines((stand.lagman, 1)), key="double-click-1")
        page = await requests_api.list_requests(limit=10, offset=0)

    assert second.duplicate and second.order.id == first.order.id
    assert page.total == 1


async def test_failure_before_request_is_finished_by_retry(
    stand: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Р-3: сбой между заказом и заявкой — заказ невидим; повтор тем же ключом
    доводит дело одной заявкой; брошенный без повтора удаляет ретеншн."""
    original = requests_api.create_request

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("database went away")

    with tenant_context(stand.tenant_id):
        monkeypatch.setattr(requests_api, "create_request", broken)
        with pytest.raises(RuntimeError):
            await _place(stand, _lines((stand.lagman, 1)), key="retry-key-1")
        with pytest.raises(RuntimeError):
            await _place(stand, _lines((stand.tea, 1)), key="abandoned-1")
        assert await room_service_api.list_active_orders_for_stay(stand.stay_id) == []
        monkeypatch.setattr(requests_api, "create_request", original)

        retried = await _place(stand, _lines((stand.lagman, 1)), key="retry-key-1")
        assert (await requests_api.list_requests(limit=10, offset=0)).total == 1
        assert await room_service_api.get_order_for_stay(retried.order.id, stand.stay_id)

        deleted = await room_service_api.delete_abandoned_orders(
            created_before=utc_now() + timedelta(seconds=1)
        )
        async with session_scope() as session:
            left = await session.scalar(select(func.count()).select_from(RoomServiceOrder))

    assert deleted == 1  # брошенный — без заявки; дооформленный остался
    assert left == 1


async def test_rejections_003_004_005_001(stand: Stand) -> None:
    with tenant_context(stand.tenant_id):
        lines = _lines((stand.lagman, 1))
        seen = (
            await room_service_api.quote(QuoteRequest(catalog=RESTAURANT, lines=lines))
        ).total_minor
        await room_service_api.update_item(
            stand.lagman,
            ItemWrite(
                section_id=(await room_service_api.get_catalog(RESTAURANT)).sections[0].id,
                names=LocalizedText(en="Lagman", ru="Лагман"),
                price_minor=3600_00,
            ),
        )
        changed = await _code(_place(stand, lines, total=seen))
        await room_service_api.set_available(stand.tea, False)
        sold_out = await _code(_place(stand, _lines((stand.tea, 1)), total=900_00))
        await room_service_api.set_available(stand.tea, True)
        await room_service_api.set_hidden(stand.tea, True)
        hidden = await _code(
            room_service_api.quote(QuoteRequest(catalog=RESTAURANT, lines=_lines((stand.tea, 1))))
        )
        foreign = await _code(_place(stand, _lines((stand.charger, 1)), total=4500_00))
        await room_service_api.set_accepting(RESTAURANT, False)
        closed = await _code(_place(stand, lines, total=3600_00 + 360_00))

    assert changed == room_service_api.ERR_ROOMSERVICE_TOTAL_CHANGED
    assert sold_out == hidden == room_service_api.ERR_ROOMSERVICE_ITEM_UNAVAILABLE
    assert foreign == room_service_api.ERR_ROOMSERVICE_NOT_FOUND
    assert closed == room_service_api.ERR_ROOMSERVICE_NOT_ACCEPTING


async def test_stay_limits_open_and_per_hotel_day(stand: Stand) -> None:
    """≤ 3 открытых заказов; ≤ 10 за сутки отеля, отменённые тоже в счёт (Н-10)."""
    with tenant_context(stand.tenant_id):
        placed = [await _place(stand, _lines((stand.tea, 1))) for _ in range(3)]
        too_many_open = await _code(_place(stand, _lines((stand.tea, 1))))
        for order in placed:
            await room_service_api.cancel_order_by_guest(order.order.id, stand.stay_id)
        for _ in range(7):
            done = await _place(stand, _lines((stand.tea, 1)))
            await room_service_api.cancel_order_by_guest(done.order.id, stand.stay_id)
        too_many_today = await _code(_place(stand, _lines((stand.tea, 1))))
        neighbour = await _place(stand, _lines((stand.tea, 1)), stay_id=stand.neighbour_stay_id)

    assert too_many_open == too_many_today == room_service_api.ERR_ROOMSERVICE_ORDER_LIMIT
    assert neighbour.order.stage is OrderStage.SENT  # лимит — на проживание, не на отель


async def test_guest_cancels_only_sent_order(stand: Stand) -> None:
    with tenant_context(stand.tenant_id):
        sent = await _place(stand, _lines((stand.tea, 1)))
        cancelled = await room_service_api.cancel_order_by_guest(sent.order.id, stand.stay_id)
        again = await room_service_api.cancel_order_by_guest(sent.order.id, stand.stay_id)
        accepted = await _place(stand, _lines((stand.lagman, 1)))
        await requests_api.change_request_status(
            accepted.order.request_id, requests_api.RequestStatus.IN_PROGRESS
        )
        refused = await _code(
            room_service_api.cancel_order_by_guest(accepted.order.id, stand.stay_id)
        )
        still = await room_service_api.get_order_for_stay(accepted.order.id, stand.stay_id)

    assert cancelled.stage is OrderStage.CANCELLED
    assert cancelled.cancel_reason is OrderCancelReason.BY_GUEST
    assert again.stage is OrderStage.CANCELLED
    assert refused == room_service_api.ERR_ROOMSERVICE_ALREADY_ACCEPTED
    assert still.stage is OrderStage.ACCEPTED and still.cancel_reason is None


async def test_stage_follows_request_and_staff_cancel_keeps_reason(stand: Stand) -> None:
    """Р-4: «Принят» → «Оплачен» → «Доставлен»; отмена сотрудником с причиной,
    чужая служба — отказ, и причина возвращается прежней."""
    waiter = await _waiter()
    with tenant_context(stand.tenant_id):
        order = (await _place(stand, _lines((stand.lagman, 1)))).order
        await requests_api.change_request_status(
            order.request_id, requests_api.RequestStatus.IN_PROGRESS, acting_user=waiter
        )
        accepted = await room_service_api.get_order_by_request(order.request_id)
        await requests_api.mark_request_paid(order.request_id, acting_user=waiter)
        paid = await room_service_api.get_order_by_request(order.request_id)
        await requests_api.change_request_status(
            order.request_id, requests_api.RequestStatus.DONE, acting_user=waiter
        )
        delivered = await room_service_api.get_order_by_request(order.request_id)

        other = (await _place(stand, _lines((stand.tea, 1)))).order
        foreign = await _code(
            room_service_api.cancel_order_by_staff(
                other.id,
                OrderCancelReason.NO_ANSWER,
                acting_user=waiter,
                allowed_category_ids={uuid.uuid4()},
            )
        )
        kept = await room_service_api.get_order_by_request(other.request_id)
        cancelled = await room_service_api.cancel_order_by_staff(
            other.id, OrderCancelReason.NO_ANSWER, acting_user=waiter
        )

    assert accepted is not None and accepted.stage is OrderStage.ACCEPTED
    assert paid is not None and paid.stage is OrderStage.PAID and paid.paid_at is not None
    assert delivered is not None and delivered.stage is OrderStage.DELIVERED
    assert foreign == requests_api.ERR_REQUESTS_FOREIGN_SERVICE
    assert kept is not None and kept.cancel_reason is None
    assert cancelled.stage is OrderStage.CANCELLED
    assert cancelled.cancel_reason is OrderCancelReason.NO_ANSWER


async def test_order_of_other_stay_or_tenant_is_not_found(stand: Stand) -> None:
    """P-4 и чужое проживание: 008, а не чужой заказ."""
    with tenant_context(stand.tenant_id):
        order = (await _place(stand, _lines((stand.lagman, 1)))).order
        neighbour = await _code(
            room_service_api.get_order_for_stay(order.id, stand.neighbour_stay_id)
        )
        neighbour_cancel = await _code(
            room_service_api.cancel_order_by_guest(order.id, stand.neighbour_stay_id)
        )
    with tenant_context(stand.other_tenant_id):
        other_tenant = await _code(room_service_api.get_order_for_stay(order.id, stand.stay_id))
        by_request = await room_service_api.get_order_by_request(order.request_id)
        async with session_scope() as session:
            orders = await session.scalar(select(func.count()).select_from(RoomServiceOrder))
            lines = await session.scalar(select(func.count()).select_from(RoomServiceOrderLine))

    assert neighbour == neighbour_cancel == other_tenant
    assert other_tenant == room_service_api.ERR_ROOMSERVICE_ORDER_NOT_FOUND
    assert by_request is None
    assert (orders, lines) == (0, 0)


async def test_retention_detaches_old_orders_from_guest(stand: Stand) -> None:
    """Через 90 дней связь заказа с проживанием и гостем обнуляется, состав
    и суммы остаются (ревью PR #497, Н-2)."""
    with tenant_context(stand.tenant_id):
        order = (await _place(stand, _lines((stand.lagman, 1)))).order
        detached = await room_service_api.detach_expired_orders(
            created_before=utc_now() + timedelta(seconds=1)
        )
        again = await room_service_api.detach_expired_orders(
            created_before=utc_now() + timedelta(seconds=1)
        )
        kept = await room_service_api.get_order_by_request(order.request_id)
        async with session_scope() as session:
            row = await session.get(RoomServiceOrder, order.id)
            assert row is not None
            link = (row.stay_id, row.guest_identity_id)

    assert (detached, again) == (1, 0)
    assert link == (None, None)
    assert kept is not None and kept.total_minor == order.total_minor and kept.lines
