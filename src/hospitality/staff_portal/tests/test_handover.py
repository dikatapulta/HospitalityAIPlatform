"""Передача заявки в кабинете (spec 0042 §6 п. 5, issue #216): кнопки по
статусу, форма передачи без текущей службы, JSON-действия «вернуть в очередь»
и «передать» под CSRF-щитом, ошибки конвертом и строка журнала изменений
«Уборка → Инженер» на странице менеджера.
"""

from __future__ import annotations

import uuid

import httpx
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)

SAME_ORIGIN = {"origin": "https://test"}
QUEUE_FRAGMENT = f"/staff/{HOTEL_SLUG}/requests/fragment"


async def _categories(
    tenant_id: uuid.UUID,
) -> tuple[requests_api.RequestCategoryRead, requests_api.RequestCategoryRead]:
    with tenant_context(tenant_id):
        housekeeping = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        engineering = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="engineering", name="Инженер")
        )
    return housekeeping, engineering


async def _request(tenant_id: uuid.UUID, category_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="течёт кран",
                room_number="305",
            )
        )


async def _post(
    client: AsyncClient, request_id: uuid.UUID, action: str, payload: dict[str, str] | None = None
) -> httpx.Response:
    return await client.post(
        f"/staff/{HOTEL_SLUG}/api/requests/{request_id}/{action}",
        json=payload or {},
        headers=SAME_ORIGIN,
    )


async def test_buttons_follow_status_and_form_excludes_current_service(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§4: у новой — «Передать» без «Вернуть в очередь», у взятой — обе и
    подсказка о возврате; в списке формы нет текущей службы."""
    housekeeping, engineering = await _categories(portal_hotel.tenant_id)
    request = await _request(portal_hotel.tenant_id, housekeeping.id)
    await submit_login(client, portal_hotel.login)

    fresh = (await client.get(QUEUE_FRAGMENT)).text
    assert 'data-action="transfer">Передать</button>' in fresh
    assert 'data-action="requeue"' not in fresh
    assert "В какую службу передать?" in fresh
    assert f'<option value="{engineering.id}">Инженер</option>' in fresh
    assert f'<option value="{housekeeping.id}">' not in fresh
    assert "Заявка вернётся в очередь" not in fresh

    assert (await _post(client, request.id, "claim")).status_code == 200
    claimed = (await client.get(QUEUE_FRAGMENT)).text
    assert 'data-action="requeue">Вернуть в очередь</button>' in claimed
    assert 'data-action="transfer"' in claimed
    assert "Заявка вернётся в очередь — её возьмёт сотрудник новой службы." in claimed


async def test_single_service_hotel_has_no_transfer(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Передавать некуда — кнопки и формы нет, а не пустой список."""
    with tenant_context(portal_hotel.tenant_id):
        only = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
    await _request(portal_hotel.tenant_id, only.id)
    await submit_login(client, portal_hotel.login)
    fragment = (await client.get(QUEUE_FRAGMENT)).text
    assert 'data-action="transfer"' not in fragment
    assert "data-transfer-form" not in fragment


async def test_requeue_clears_claim_and_card_shows_new(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    housekeeping, _ = await _categories(portal_hotel.tenant_id)
    request = await _request(portal_hotel.tenant_id, housekeeping.id)
    await submit_login(client, portal_hotel.login)
    assert (await _post(client, request.id, "claim")).status_code == 200

    response = await _post(client, request.id, "requeue")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "new"
    assert payload["claimed_by_display_name"] is None
    assert payload["claimed_at"] is None
    fragment = (await client.get(QUEUE_FRAGMENT)).text
    assert "Взял:" not in fragment
    assert 'data-action="claim">Взять</button>' in fragment

    again = await _post(client, request.id, "requeue")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR-REQUESTS-003"


async def test_transfer_moves_request_and_conflicts_are_enveloped(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    housekeeping, engineering = await _categories(portal_hotel.tenant_id)
    request = await _request(portal_hotel.tenant_id, housekeeping.id)
    await submit_login(client, portal_hotel.login)
    assert (await _post(client, request.id, "claim")).status_code == 200

    moved = await _post(client, request.id, "transfer", {"category_id": str(engineering.id)})
    assert moved.status_code == 200
    assert moved.json()["category_id"] == str(engineering.id)
    assert moved.json()["status"] == "new"
    assert moved.json()["daily_number"] == request.daily_number

    same = await _post(client, request.id, "transfer", {"category_id": str(engineering.id)})
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "ERR-REQUESTS-005"
    unknown = await _post(client, request.id, "transfer", {"category_id": str(uuid.uuid4())})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "ERR-REQUESTS-001"
    malformed = await _post(client, request.id, "transfer", {"category_id": "kitchen"})
    assert malformed.status_code == 422


async def test_handover_actions_enforce_csrf_contract(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Канон JSON-действий: без Origin — 403, заявка нетронута."""
    housekeeping, engineering = await _categories(portal_hotel.tenant_id)
    request = await _request(portal_hotel.tenant_id, housekeeping.id)
    await submit_login(client, portal_hotel.login)
    base = f"/staff/{HOTEL_SLUG}/api/requests/{request.id}"

    for path, body in (
        (f"{base}/transfer", {"category_id": str(engineering.id)}),
        (f"{base}/requeue", {}),
    ):
        response = await client.post(path, json=body)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "ERR-AUTH-009"
    with tenant_context(portal_hotel.tenant_id):
        stored = await requests_api.get_request(request.id)
    assert stored.category_id == housekeeping.id


async def test_changes_page_shows_requeue_and_transfer(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """§2.3: журнал менеджера — «Возвращена в очередь» и «Передана в другую
    службу» с «откуда → куда»; фильтр «Действие» знает обе подписи."""
    await store_hotel_config(portal_hotel.tenant_id)
    housekeeping, engineering = await _categories(portal_hotel.tenant_id)
    request = await _request(portal_hotel.tenant_id, housekeeping.id)
    await submit_login(client, portal_hotel.login)
    assert (await _post(client, request.id, "claim")).status_code == 200
    assert (await _post(client, request.id, "requeue")).status_code == 200
    transfer = await _post(client, request.id, "transfer", {"category_id": str(engineering.id)})
    assert transfer.status_code == 200

    page = (await client.get(f"/staff/{HOTEL_SLUG}/changes")).text
    number = f"#{request.daily_number}"
    assert "<td>Возвращена в очередь</td>" in page
    assert "<td>Передана в другую службу</td>" in page
    assert f"{number} · Уборка → Инженер · 305" in page
    assert '<option value="request_transferred"' in page
    only_transfers = (
        await client.get(f"/staff/{HOTEL_SLUG}/changes", params={"action": "request_transferred"})
    ).text
    assert only_transfers.count("<tr><td>") == 1
