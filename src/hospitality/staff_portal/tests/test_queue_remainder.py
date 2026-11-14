"""«Готово» с остатком из кабинета (spec 0079 §2.3, issue #58).

Галочка «доделать остальное позже» шлёт `remainder: true` вместе с примечанием:
исходная закрывается, остаток встаёт в очередь той же службы, а ответ несёт его
`#N` для всплывашки. Без галочки — прежнее «Готово».
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


async def _claimed_request(
    client: AsyncClient, portal_hotel: PortalHotel
) -> requests_api.ServiceRequestRead:
    with tenant_context(portal_hotel.tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="уборка номера",
                room_number="305",
            )
        )
    await submit_login(client, portal_hotel.login)
    assert (await _post(client, request.id, "claim", {})).status_code == 200
    return request


async def _post(
    client: AsyncClient, request_id: uuid.UUID, action: str, payload: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        f"/staff/{HOTEL_SLUG}/api/requests/{request_id}/{action}",
        json=payload,
        headers=SAME_ORIGIN,
    )


async def test_complete_with_remainder_queues_the_rest(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    request = await _claimed_request(client, portal_hotel)

    response = await _post(
        client, request.id, "complete", {"note": "окно не помыто", "remainder": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["resolution_note"] == "окно не помыто"
    with tenant_context(portal_hotel.tenant_id):
        (remainder,) = await requests_api.list_open_requests(limit=10)
    assert body["remainder_daily_number"] == remainder.daily_number
    assert remainder.parent_request_id == request.id
    assert remainder.origin is requests_api.ServiceRequestOrigin.REMAINDER
    assert remainder.closed_at is None

    page = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert f"Остаток заявки #{request.daily_number}" in page.text


async def test_note_without_remainder_flag_closes_as_before(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    request = await _claimed_request(client, portal_hotel)

    response = await _post(client, request.id, "complete", {"note": "гость отказался от ванной"})

    assert response.status_code == 200
    assert response.json()["remainder_daily_number"] is None
    with tenant_context(portal_hotel.tenant_id):
        assert await requests_api.list_open_requests(limit=10) == []


async def test_remainder_without_note_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    request = await _claimed_request(client, portal_hotel)

    response = await _post(client, request.id, "complete", {"note": "  ", "remainder": True})

    assert response.status_code == 422
    with tenant_context(portal_hotel.tenant_id):
        assert (await requests_api.get_request(request.id)).status is (
            requests_api.RequestStatus.IN_PROGRESS
        )


async def test_note_form_carries_remainder_checkbox(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _claimed_request(client, portal_hotel)

    page = await client.get(f"/staff/{HOTEL_SLUG}/requests")

    assert 'name="remainder" checked' in page.text
    assert "Доделать остальное позже — новая заявка в очередь службы" in page.text
