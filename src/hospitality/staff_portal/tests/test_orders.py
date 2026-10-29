"""Шаги заказа в номер в кабинете (spec 0038 §5, §6 PR 6; issue #418).

Карточка заказа в очереди: «🍽 Заказ #N · № комнаты · итог», состав, суммы,
пожелания, кто и когда нажал шаг; кнопка только следующего шага — «Иду за оплатой» →
«Оплачено» (заказ принят) → «Доставлено» — и «Отменить» с причиной. «Доставлено» до оплаты —
ERR-REQUESTS-006, заказ открыт; у оплаченного «Отказался платить» нет (009);
чужая служба — 007; не заказ и чужой тенант — 008; без `Origin` — ERR-AUTH-009.
Сигнал помечает заказ — уведомление «Новый заказ #N».
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import LocalizedText
from hospitality.platform.models import StaffRole, Tenant
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.staff_portal.tests.test_catalog import (
    SAME_ORIGIN,
    _configure,
    _hotel,
    _room_service,
)
from tests.test_staff_auth import create_staff_user, unique_login

QUEUE = f"/staff/{HOTEL_SLUG}/requests"
FRAGMENT = f"{QUEUE}/fragment"
API = f"/staff/{HOTEL_SLUG}/api"
NBSP = " "

Catalog = room_service_api.RoomServiceCatalog
Reason = room_service_api.OrderCancelReason
Stage = room_service_api.OrderStage


@dataclass(frozen=True)
class Stand:
    tenant_id: uuid.UUID
    stay_id: uuid.UUID
    lagman: uuid.UUID
    tea: uuid.UUID


async def _stand(tenant_id: uuid.UUID) -> Stand:
    """Проживание в 512 и ресторан с двумя блюдами, «принимаем» включено."""
    with tenant_context(tenant_id):
        stay = await check_in(
            StayCheckIn(
                room_number="512",
                check_out_at=utc_now() + timedelta(days=2),
                guest_display_name="Wang Li",
            )
        )
        section = await room_service_api.create_section(
            Catalog.RESTAURANT,
            room_service_api.SectionWrite(names=LocalizedText(en="Mains", ru="Горячее")),
        )
        items = [
            await room_service_api.create_item(
                room_service_api.ItemWrite(
                    section_id=section.id,
                    names=LocalizedText(en=en, ru=ru),
                    price_minor=tenge * 100,
                )
            )
            for en, ru, tenge in (("Lagman", "Лагман", 3400), ("Tea", "Чай", 900))
        ]
        await room_service_api.set_accepting(Catalog.RESTAURANT, True)
    return Stand(tenant_id, stay.stay.id, items[0].id, items[1].id)


async def _order(stand: Stand, item_id: uuid.UUID) -> room_service_api.OrderRead:
    lines = [room_service_api.CartLine(item_id=item_id, quantity=1)]
    with tenant_context(stand.tenant_id):
        quote = await room_service_api.quote(
            room_service_api.QuoteRequest(catalog=Catalog.RESTAURANT, lines=lines)
        )
        placed = await room_service_api.place_order(
            room_service_api.OrderPlace(
                catalog=Catalog.RESTAURANT,
                stay_id=stand.stay_id,
                guest_identity_id=uuid.uuid4(),
                client_order_key=uuid.uuid4().hex,
                guest_language="en",
                lines=lines,
                comment="без лука",
                expected_total_minor=quote.total_minor,
            )
        )
    return placed.order


@pytest.fixture
async def stand(portal_hotel: PortalHotel) -> Stand:
    await _hotel(portal_hotel)
    return await _stand(portal_hotel.tenant_id)


async def _login(
    client: AsyncClient, portal_hotel: PortalHotel, services: tuple[str, ...], name: str
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name=name,
        service_keys=services,
    )
    await submit_login(client, staff_login)


async def _post(client: AsyncClient, path: str, body: object | None = None) -> httpx.Response:
    return await client.post(f"{API}{path}", json=body or {}, headers=SAME_ORIGIN)


def _money(tenge: str) -> str:
    return f"{tenge.replace(' ', NBSP)}{NBSP}₸"


async def _status(stand: Stand, request_id: uuid.UUID) -> requests_api.RequestStatus:
    with tenant_context(stand.tenant_id):
        return (await requests_api.get_request(request_id)).status


async def test_card_walks_accept_paid_delivered_with_names(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand
) -> None:
    """«Иду за оплатой» → «Оплачено» → «Доставлено»: у карточки один следующий шаг,
    имена и время шагов, «принят» — только после оплаты; «Доставлено» до оплаты —
    006, заказ остаётся в работе."""
    order = await _order(stand, stand.lagman)
    rid = order.request_id
    await _login(client, portal_hotel, ("fnb",), "Айгерим")

    sent = (await client.get(QUEUE)).text
    claimed = await _post(client, f"/requests/{rid}/claim")
    accepted = (await client.get(FRAGMENT)).text
    early = await _post(client, f"/requests/{rid}/complete")
    early_status = await _status(stand, rid)
    paid = await _post(client, f"/orders/{rid}/paid")
    paid_card = (await client.get(FRAGMENT)).text
    delivered = await _post(client, f"/requests/{rid}/complete")
    closed = (await client.get(f"{FRAGMENT}?tab=closed")).text

    assert f"🍽 Заказ #1 · № 512 · {_money('3 740')}" in sent
    assert f"Лагман ×1 — {_money('3 400')}" in sent
    assert f"Сумма {_money('3 400')} · сбор 10% — {_money('340')} · итого {_money('3 740')}" in sent
    assert "Пожелания: без лука" in sent
    assert 'data-action="claim">Иду за оплатой<' in sent
    for generic in ('data-action="complete"', 'data-action="requeue"', 'data-action="transfer"'):
        assert generic not in sent
    assert 'data-action="cancel"' not in sent  # общая отмена с запиской (Н-10)
    assert claimed.status_code == 200
    assert 'data-action="paid">Оплачено<' in accepted
    assert "заказ будет принят. Затем пробейте его в кассе ресторана" in accepted
    assert ">за оплатой<" in accepted
    assert 'data-action="deliver"' not in accepted
    assert "За оплатой: Айгерим, " in accepted
    assert early.status_code == 409
    assert early.json()["error"]["code"] == requests_api.ERR_REQUESTS_DONE_WITHOUT_PAYMENT
    assert early_status is requests_api.RequestStatus.IN_PROGRESS
    assert paid.status_code == 200 and paid.json()["stage"] == Stage.PAID
    assert 'data-action="deliver">Доставлено<' in paid_card
    assert 'data-action="paid"' not in paid_card
    assert "Оплачено: Айгерим, " in paid_card and ">принят<" in paid_card
    assert delivered.status_code == 200
    assert "Доставлено: Айгерим, " in closed and ">доставлен<" in closed


async def test_cancel_with_reason_and_paid_order_keeps_payment_refused_out(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand
) -> None:
    """Отмена — причиной из списка; у оплаченного «Отказался платить» в списке
    нет, а прямой запрос с ней — 009 (ревью PR #497, Н-7); «гость сам» — 422."""
    fresh = await _order(stand, stand.lagman)
    paid = await _order(stand, stand.tea)
    await _login(client, portal_hotel, ("fnb",), "Айгерим")
    await _post(client, f"/requests/{paid.request_id}/claim")
    await _post(client, f"/orders/{paid.request_id}/paid")

    page = (await client.get(QUEUE)).text
    fresh_card = page.split(f'data-request-id="{fresh.request_id}"')[1]
    paid_card = page.split(f'data-request-id="{paid.request_id}"')[1].split("</article>")[0]
    by_guest = await _post(client, f"/orders/{fresh.request_id}/cancel", {"reason": "by_guest"})
    cancelled = await _post(client, f"/orders/{fresh.request_id}/cancel", {"reason": "no_answer"})
    refused = await _post(
        client, f"/orders/{paid.request_id}/cancel", {"reason": "payment_refused"}
    )
    unavailable = await _post(
        client, f"/orders/{paid.request_id}/cancel", {"reason": "item_unavailable"}
    )

    assert 'value="payment_refused">Отказался платить<' in fresh_card.split("</article>")[0]
    assert 'value="payment_refused"' not in paid_card
    assert 'value="item_unavailable">Нет блюда<' in paid_card
    assert "Оплата отмечена — верните деньги гостю через кассу ресторана" in paid_card
    assert by_guest.status_code == 422
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_reason"] == Reason.NO_ANSWER
    closed = (await client.get(f"{FRAGMENT}?tab=closed")).text
    assert "Отменён: Айгерим, " in closed and " · Не открыли дверь" in closed
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_REASON_NOT_FOR_PAID
    assert unavailable.status_code == 200
    assert unavailable.json()["stage"] == Stage.CANCELLED
    assert unavailable.json()["cancel_reason"] == Reason.ITEM_UNAVAILABLE


async def test_foreign_service_non_order_and_no_origin_are_refused(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand
) -> None:
    """Снабжение не трогает заказ ресторана (007, причина не записана); заявка не
    заказ — 008; JSON без `Origin` — ERR-AUTH-009."""
    order = await _order(stand, stand.lagman)
    with tenant_context(stand.tenant_id):
        fnb = next(c for c in await requests_api.list_categories() if c.key == "fnb")
        plain = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=fnb.id,
                origin=requests_api.ServiceRequestOrigin.API,
                summary="полотенца",
                room_number="512",
            )
        )
    await _login(client, portal_hotel, ("supply",), "Ержан")

    paid = await _post(client, f"/orders/{order.request_id}/paid")
    cancel = await _post(client, f"/orders/{order.request_id}/cancel", {"reason": "no_answer"})
    with tenant_context(stand.tenant_id):
        kept = await room_service_api.get_order_by_request(order.request_id)
    not_order = await _post(client, f"/orders/{plain.id}/paid")
    no_origin = await client.post(f"{API}/orders/{order.request_id}/paid", json={})

    assert paid.status_code == cancel.status_code == 403
    assert paid.json()["error"]["code"] == requests_api.ERR_REQUESTS_FOREIGN_SERVICE
    assert cancel.json()["error"]["code"] == requests_api.ERR_REQUESTS_FOREIGN_SERVICE
    assert kept is not None and kept.stage is Stage.SENT and kept.cancel_reason is None
    assert not_order.status_code == 404
    assert not_order.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_ORDER_NOT_FOUND
    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "ERR-AUTH-009"


async def test_order_of_other_tenant_is_not_found(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand
) -> None:
    """P-4: менеджер отеля A по `request_id` заказа отеля B получает 008, заказ B цел."""
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
        other_id = other.id
    with tenant_context(other_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="fnb", name="Ресторан")
        )
    await _configure(other_id, _room_service())
    other_stand = await _stand(other_id)
    foreign = await _order(other_stand, other_stand.lagman)
    await submit_login(client, portal_hotel.login)

    paid = await _post(client, f"/orders/{foreign.request_id}/paid")
    cancel = await _post(client, f"/orders/{foreign.request_id}/cancel", {"reason": "other"})
    with tenant_context(other_id):
        kept = await room_service_api.get_order_by_request(foreign.request_id)

    assert paid.status_code == cancel.status_code == 404
    assert paid.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_ORDER_NOT_FOUND
    assert kept is not None and kept.stage is Stage.SENT


async def test_signal_marks_orders(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand
) -> None:
    """Сигнал: у заказа `is_order` — уведомление «Новый заказ #N»; заявка — нет."""
    order = await _order(stand, stand.lagman)
    with tenant_context(stand.tenant_id):
        fnb = next(c for c in await requests_api.list_categories() if c.key == "fnb")
        plain = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=fnb.id,
                origin=requests_api.ServiceRequestOrigin.API,
                summary="полотенца",
                room_number="512",
            )
        )
    await _login(client, portal_hotel, ("fnb",), "Айгерим")

    response = await client.get(f"{API}/requests/signal")

    flags = {item["id"]: item["is_order"] for item in response.json()["unclaimed"]}
    assert flags == {str(order.request_id): True, str(plain.id): False}
