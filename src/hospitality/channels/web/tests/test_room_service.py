"""«Заказать в номер» в веб-чате (spec 0038 §6 PR 5): маршруты гостя за сессией,
флаг выключен — 404 и нет кнопки, чужой заказ — 008, сообщения о шагах заказа —
по одному на шаг и на языке заказа."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.web.menu import messages
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, RECEPTION_PHONE, ROOM, WebHotel
from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    RoomServiceCatalogConfig,
    RoomServiceConfig,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.tenancy import tenant_context

BASE = f"/g/{HOTEL_SLUG}/{ROOM}"


@dataclass(frozen=True)
class Menu:
    hotel: WebHotel
    lagman: uuid.UUID
    tea: uuid.UUID


async def _store_config(tenant_id: uuid.UUID, room_service: RoomServiceConfig | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=RECEPTION_PHONE,
                room_service=room_service,
            ),
        )


@pytest.fixture
async def menu(web_hotel: WebHotel) -> Menu:
    await _store_config(
        web_hotel.tenant_id,
        RoomServiceConfig(
            enabled=True,
            catalogs={
                "restaurant": RoomServiceCatalogConfig(
                    category_key="fnb", service_charge_percent=Decimal("10")
                )
            },
        ),
    )
    with tenant_context(web_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="fnb", name="F&B")
        )
        section = await room_service_api.create_section(
            room_service_api.RoomServiceCatalog.RESTAURANT,
            room_service_api.SectionWrite(names=LocalizedText(en="Mains")),
        )
        items = [
            await room_service_api.create_item(
                room_service_api.ItemWrite(
                    section_id=section.id,
                    names=LocalizedText(en=en, ru=ru),
                    price_minor=tenge * 100,
                )
            )
            for en, ru, tenge in (("Lagman", "Лагман", 3400), ("Tea <b>", "Чай", 900))
        ]
        await room_service_api.set_accepting(room_service_api.RoomServiceCatalog.RESTAURANT, True)
    return Menu(web_hotel, items[0].id, items[1].id)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        yield c


async def _enter(client: AsyncClient, code: str, base: str = BASE) -> None:
    response = await client.post(f"{base}/session", json={"code": code})
    assert response.status_code == 200, response.text


def _order_body(
    item_id: uuid.UUID, total: int, *, key: str = "key-0001", language: str = "en"
) -> dict[str, object]:
    return {
        "catalog": "restaurant",
        "client_order_key": key,
        "language": language,
        "lines": [{"item_id": str(item_id), "quantity": 1}],
        "comment": "no onions",
        "expected_total_minor": total,
    }


async def _outbound(client: AsyncClient) -> list[str]:
    response = await client.get(f"{BASE}/messages")
    assert response.status_code == 200
    return [m["text"] for m in response.json()["messages"] if m["direction"] == "outbound"]


async def test_disabled_room_service_has_no_button_and_404(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    await _enter(client, web_hotel.access_code)
    chat = await client.get(BASE)
    page = await client.get(f"{BASE}/menu")
    data = await client.get(f"{BASE}/menu/data")

    assert chat.status_code == 200 and 'id="orders-link"' not in chat.text
    assert page.status_code == data.status_code == 404
    assert data.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_CATALOG_DISABLED


async def test_guest_pages_need_the_session(client: AsyncClient, menu: Menu) -> None:
    chat = await client.get(BASE)
    page = await client.get(f"{BASE}/menu", follow_redirects=False)
    data = await client.get(f"{BASE}/menu/data")

    assert 'id="orders-link"' in chat.text and "Заказать в номер" in chat.text
    assert page.status_code == 303 and page.headers["location"] == BASE
    assert data.status_code == 401

    await _enter(client, menu.hotel.access_code)
    page = await client.get(f"{BASE}/menu")
    assert page.status_code == 200 and 'id="menu-texts"' in page.text


async def test_menu_quote_order_cancel_over_http(client: AsyncClient, menu: Menu) -> None:
    await _enter(client, menu.hotel.access_code)
    data = (await client.get(f"{BASE}/menu/data")).json()
    quote = await client.post(
        f"{BASE}/menu/quote",
        json={"catalog": "restaurant", "lines": [{"item_id": str(menu.lagman), "quantity": 1}]},
    )
    total = quote.json()["total_minor"]
    created = await client.post(f"{BASE}/orders", json=_order_body(menu.lagman, total))
    repeated = await client.post(f"{BASE}/orders", json=_order_body(menu.lagman, total))
    order_id = created.json()["id"]
    fetched = await client.get(f"{BASE}/orders/{order_id}")
    listed = (await client.get(f"{BASE}/menu/data")).json()["orders"]
    cancelled = await client.post(f"{BASE}/orders/{order_id}/cancel")
    stale = await client.post(
        f"{BASE}/orders", json=_order_body(menu.lagman, total - 100, key="key-0002")
    )

    assert data["room_number"] == ROOM
    assert [c["catalog"] for c in data["catalogs"]] == ["restaurant"]
    assert data["catalogs"][0]["sections"][0]["items"][1]["names"]["en"] == "Tea <b>"
    assert total == 3740_00
    assert created.status_code == 201 and repeated.status_code == 200
    assert repeated.json()["id"] == order_id
    assert fetched.json()["stage"] == "sent" and fetched.json()["comment"] == "no onions"
    assert [o["id"] for o in listed] == [order_id]
    assert cancelled.json()["stage"] == "cancelled"
    assert cancelled.json()["cancel_reason"] == "by_guest"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_TOTAL_CHANGED


async def test_order_of_another_stay_is_not_found(menu: Menu) -> None:
    with tenant_context(menu.hotel.tenant_id):
        neighbour = await check_in(
            StayCheckIn(
                room_number="102",
                check_out_at=utc_now() + timedelta(days=1),
                guest_display_name="Anna",
            )
        )
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as guest,
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other,
    ):
        await _enter(guest, menu.hotel.access_code)
        await _enter(other, neighbour.access_code, f"/g/{HOTEL_SLUG}/102")
        order = await guest.post(f"{BASE}/orders", json=_order_body(menu.tea, 990_00))
        peek = await other.get(f"/g/{HOTEL_SLUG}/102/orders/{order.json()['id']}")
        cancel = await other.post(f"/g/{HOTEL_SLUG}/102/orders/{order.json()['id']}/cancel")

    assert order.status_code == 201
    assert peek.status_code == cancel.status_code == 404
    assert peek.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_ORDER_NOT_FOUND


async def test_each_step_message_once_in_order_language(client: AsyncClient, menu: Menu) -> None:
    """Оформлен → Принял (дважды после возврата в очередь — одно сообщение) →
    Оплачено → Доставлено (примечание к «Готово» гостю не уходит); отмена гостем —
    молча, сотрудником без причины — «Другое»; обычная заявка — не заказ, подписчик
    молчит."""
    messages.register()
    async with platform_session_scope() as session:
        user = User(display_name="Aigerim")
        session.add(user)
        await session.flush()
        waiter = requests_api.ActingUser(user_id=user.id, display_name="Aigerim")
    await _enter(client, menu.hotel.access_code)
    first = (await client.post(f"{BASE}/orders", json=_order_body(menu.lagman, 3740_00))).json()
    second = (
        await client.post(
            f"{BASE}/orders", json=_order_body(menu.tea, 990_00, key="key-0002", language="ru")
        )
    ).json()
    third = (
        await client.post(f"{BASE}/orders", json=_order_body(menu.tea, 990_00, key="key-0003"))
    ).json()
    status = requests_api.RequestStatus
    with tenant_context(menu.hotel.tenant_id):
        request_id = uuid.UUID(first["request_id"])
        await deliver_pending_events()
        await requests_api.change_request_status(request_id, status.IN_PROGRESS, acting_user=waiter)
        await requests_api.change_request_status(request_id, status.NEW)
        await requests_api.change_request_status(request_id, status.IN_PROGRESS, acting_user=waiter)
        await requests_api.mark_request_paid(request_id, acting_user=waiter)
        await requests_api.change_request_status(
            request_id, status.DONE, acting_user=waiter, resolution_note="no lemon today"
        )
        await room_service_api.cancel_order_by_guest(uuid.UUID(second["id"]), menu.hotel.stay_id)
        await requests_api.change_request_status(uuid.UUID(third["request_id"]), status.CANCELLED)
        housekeeping = next(
            c for c in await requests_api.list_categories() if c.key == "housekeeping"
        )
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=housekeeping.id,
                origin=requests_api.ServiceRequestOrigin.API,
                summary="towels",
                room_number=ROOM,
            )
        )
        while await deliver_pending_events():
            pass

    texts = await _outbound(client)
    assert texts == [
        "Order #1 has been sent to the restaurant: Lagman ×1. Total 3,740 ₸ including service "
        "charge. Once the restaurant accepts it, a staff member will come to you for payment — "
        "cash or card. We start cooking after payment.",
        "Заказ #2 отправлен в ресторан: Чай ×1. Итого 990 ₸ с сервисным сбором. Как только "
        "ресторан примет заказ, сотрудник придёт к вам за оплатой — наличными или картой. "
        "Готовить начнём после оплаты.",
        "Order #3 has been sent to the restaurant: Tea <b> ×1. Total 990 ₸ including service "
        "charge. Once the restaurant accepts it, a staff member will come to you for payment — "
        "cash or card. We start cooking after payment.",
        "The restaurant has accepted order #1. A staff member will come shortly for payment — "
        "3,740 ₸, cash or card.",
        "Payment received — we're cooking. We'll bring it to room 101.",
        # Примечание к «Готово» гостю не уходит (ревью PR #520, Н-9).
        "Order #1 has been delivered. Enjoy your meal!",
        "Order #3 has been cancelled: the restaurant can't fulfil the order. If this is a "
        "mistake, write to us.",
    ]


async def test_staff_cancel_reason_reaches_guest_with_refund_after_payment(
    client: AsyncClient, menu: Menu
) -> None:
    """PR 6: причина отмены сотрудником — словами гостя; после «Оплачено» —
    перед последней фразой строка о возврате денег (spec 0038 §5)."""
    messages.register()
    async with platform_session_scope() as session:
        user = User(display_name="Aigerim")
        session.add(user)
        await session.flush()
        waiter = requests_api.ActingUser(user_id=user.id, display_name="Aigerim")
    await _enter(client, menu.hotel.access_code)
    first = (
        await client.post(f"{BASE}/orders", json=_order_body(menu.lagman, 3740_00, language="ru"))
    ).json()
    second = (
        await client.post(
            f"{BASE}/orders", json=_order_body(menu.tea, 990_00, key="key-0002", language="ru")
        )
    ).json()
    reason = room_service_api.OrderCancelReason
    with tenant_context(menu.hotel.tenant_id):
        await room_service_api.cancel_order_by_staff(
            uuid.UUID(first["id"]), reason.NO_ANSWER, acting_user=waiter
        )
        await requests_api.change_request_status(
            uuid.UUID(second["request_id"]),
            requests_api.RequestStatus.IN_PROGRESS,
            acting_user=waiter,
        )
        await room_service_api.mark_order_paid(uuid.UUID(second["id"]), acting_user=waiter)
        await room_service_api.cancel_order_by_staff(
            uuid.UUID(second["id"]), reason.ITEM_UNAVAILABLE, acting_user=waiter
        )
        while await deliver_pending_events():
            pass

    texts = await _outbound(client)
    assert (
        "Заказ #1 отменён: сотрудник не застал вас в номере. Если это ошибка — напишите нам."
        in texts
    )
    assert (
        "Заказ #2 отменён: одного из блюд нет в наличии. Деньги за заказ вам вернут — "
        "сотрудник свяжется с вами. Если это ошибка — напишите нам." in texts
    )
