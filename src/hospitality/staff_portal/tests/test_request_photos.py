"""Фото гостя в карточке заявки кабинета (spec 0081 §7, §10; issue #82).

Лента: значок камеры в строке и миниатюры в теле карточки (панель); заявка без
фото — без них. Маршрут фото: своя служба, ресепшен и менеджер — картинка;
чужая служба и не приложенный к заявке файл — 404 `ERR-REQUESTS-010`.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import FilePurpose, store_upload
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.staff_portal.tests.test_queue import _make_request
from hospitality.staff_portal.tests.test_service_queues import _staff
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_file_content import make_png

QUEUE = f"/staff/{HOTEL_SLUG}/requests"
FRAGMENT = f"{QUEUE}/fragment"


def _photo_url(request_id: uuid.UUID, file_id: uuid.UUID) -> str:
    return f"{QUEUE}/{request_id}/photos/{file_id}"


async def _with_photos(
    tenant_id: uuid.UUID,
    summary: str,
    count: int = 2,
    *,
    category_key: str = "housekeeping",
    room_number: str = "305",
) -> tuple[requests_api.ServiceRequestRead, list[uuid.UUID]]:
    request = await _make_request(
        tenant_id, summary, category_key=category_key, room_number=room_number
    )
    with tenant_context(tenant_id):
        photos = [
            (await store_upload(FilePurpose.REQUEST_PHOTO, make_png(), "tap.png")).id
            for _ in range(count)
        ]
        await requests_api.attach_request_photos(request.id, photos)
    return request, photos


async def test_queue_shows_camera_and_thumbnails_only_for_request_with_photos(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    request, photos = await _with_photos(portal_hotel.tenant_id, "TAP-305")
    await _make_request(portal_hotel.tenant_id, "TOWELS-306", room_number="306")
    await submit_login(client, portal_hotel.login)

    page = (await client.get(QUEUE)).text
    fragment = (await client.get(FRAGMENT)).text

    for html in (page, fragment):
        assert html.count("С фото гостя.") == 1
        assert html.count('class="request-photos"') == 1
        for number, file_id in enumerate(photos, start=1):
            url = _photo_url(request.id, file_id)
            assert f'<a href="{url}" target="_blank" rel="noopener">' in html
            assert f'<img src="{url}" alt="Фото гостя {number}"' in html


async def test_own_service_gets_image_with_safe_headers(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    request, photos = await _with_photos(portal_hotel.tenant_id, "TAP-305", count=1)
    await _staff(client, portal_hotel, "housekeeping")

    response = await client.get(_photo_url(request.id, photos[0]))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in response.headers


async def test_foreign_service_and_unlinked_file_are_404(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    request, photos = await _with_photos(portal_hotel.tenant_id, "TAP-305", count=1)
    pipe, _ = await _with_photos(
        portal_hotel.tenant_id,
        "PIPE-101",
        count=1,
        category_key="maintenance",
        room_number="101",
    )
    await _staff(client, portal_hotel, "maintenance")

    foreign = await client.get(_photo_url(request.id, photos[0]))
    unlinked = await client.get(_photo_url(pipe.id, photos[0]))
    page = (await client.get(QUEUE)).text

    for response in (foreign, unlinked):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == requests_api.ERR_REQUESTS_PHOTO_NOT_FOUND
    assert str(request.id) not in page and "PIPE-101" in page


async def test_manager_gets_any_service_photo_and_anonymous_does_not(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
) -> None:
    request, photos = await _with_photos(
        portal_hotel.tenant_id, "PIPE-101", count=1, category_key="maintenance"
    )
    await submit_login(client, portal_hotel.login)

    manager = await client.get(_photo_url(request.id, photos[0]))
    anonymous = await second_client.get(_photo_url(request.id, photos[0]))

    assert manager.status_code == 200
    assert anonymous.status_code != 200
    assert anonymous.headers.get("content-type", "") != "image/webp"
