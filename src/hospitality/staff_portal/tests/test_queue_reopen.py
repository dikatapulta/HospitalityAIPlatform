"""«Вернуть в работу» в кабинете (spec 0080 §3, issue #55).

Выполненная заявка во вкладке «Закрытые» несёт кнопку возврата, `POST …/reopen`
возвращает её в работу со строкой журнала. Ответ «Готово» говорит странице, дать ли
«Отменить» во всплывашке: запросу с витрины и закрытию с остатком — нет. Отказ домена —
409 `ERR-REQUESTS-009`. Карточки заказа и брони — `test_orders.py`, `test_bookings.py` (#998).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login

SAME_ORIGIN = {"origin": "https://test"}
Origin = requests_api.ServiceRequestOrigin


async def _claimed(
    client: AsyncClient, portal_hotel: PortalHotel, origin: Origin = Origin.GUEST_CHAT
) -> uuid.UUID:
    with tenant_context(portal_hotel.tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id, origin=origin, summary="уборка номера", room_number="305"
            )
        )
    await submit_login(client, portal_hotel.login)
    assert (await _post(client, request.id, "claim", {})).status_code == 200
    return request.id


async def _post(
    client: AsyncClient, request_id: uuid.UUID, action: str, payload: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        f"/staff/{HOTEL_SLUG}/api/requests/{request_id}/{action}",
        json=payload,
        headers=SAME_ORIGIN,
    )


async def test_done_request_is_reopened_from_the_closed_tab(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    request_id = await _claimed(client, portal_hotel)
    completed = await _post(client, request_id, "complete", {})
    assert completed.json()["can_reopen"] is True

    closed_tab = await client.get(f"/staff/{HOTEL_SLUG}/requests?tab=closed")
    assert 'data-action="reopen">Вернуть в работу' in closed_tab.text

    response = await _post(client, request_id, "reopen", {})

    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    assert response.json()["closed_at"] is None
    open_tab = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert 'data-action="reopen"' not in open_tab.text
    journal = await client.get(f"/staff/{HOTEL_SLUG}/changes")
    assert "Возвращена в работу" in journal.text


async def test_showroom_request_gets_no_undo(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    form_id = await _claimed(client, portal_hotel, Origin.SHOWROOM_FORM)
    form_done = await _post(client, form_id, "complete", {})
    refused = await _post(client, form_id, "reopen", {})

    assert form_done.json()["can_reopen"] is False
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == requests_api.ERR_REQUESTS_REOPEN_NOT_ALLOWED
    closed_tab = await client.get(f"/staff/{HOTEL_SLUG}/requests?tab=closed")
    assert 'data-action="reopen"' not in closed_tab.text


async def test_close_with_remainder_offers_no_undo(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    request_id = await _claimed(client, portal_hotel)

    response = await _post(
        client, request_id, "complete", {"note": "окно не помыто", "remainder": True}
    )

    assert response.status_code == 200
    assert response.json()["can_reopen"] is False
