"""Фото товара в панели «Товары и меню» (spec 0038 §6 п. 7, PR 7; issue #418).

Загрузка — `multipart/form-data` под своим щитом (тот же Origin, ERR-AUTH-009),
право — `can_edit_catalog` каталога товара и на загрузку, и на выдачу картинки;
потолок тела — до разбора (ERR-FILES-001), файл ровно один. Карточка показывает
фото адресом панели по id файла, без фото — заглушку и счётчик «Без фото».
Предпросмотр `blob:` разрешён только странице каталога.
"""

# ruff: noqa: F811

from __future__ import annotations

import pytest
from httpx import AsyncClient, Response

from hospitality.platform.models import StaffRole
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.staff_portal import catalog_api
from hospitality.staff_portal.tests.conftest import PortalHotel
from hospitality.staff_portal.tests.test_catalog import (
    ACTIONS,
    FRAGMENT,
    HOME,
    PAGE,
    SAME_ORIGIN,
    _hotel,
    _item,
    _login_as,
    _post,
    _section,
)
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_file_content import make_png


async def _upload(
    client: AsyncClient, item_id: str, data: bytes, *, headers: dict[str, str] | None = None
) -> Response:
    return await client.post(
        f"{ACTIONS}/items/{item_id}/photo",
        files={"photo": ("лагман.png", data, "image/png")},
        headers=SAME_ORIGIN if headers is None else headers,
    )


async def _restaurant_item(client: AsyncClient) -> str:
    section = await _section(client, "restaurant", "Mains", "Горячее")
    return await _item(client, section, "Lagman", 3400)


async def test_service_sets_replaces_and_removes_photo(
    portal_hotel: PortalHotel, client: AsyncClient, fake_file_storage: FileStoragePort
) -> None:
    await _hotel(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("fnb",))
    item_id = await _restaurant_item(client)

    first = await _upload(client, item_id, make_png((1600, 1200)))
    first_id = first.json()["photo_file_id"]
    board = (await client.get(FRAGMENT)).text
    image = await client.get(f"{ACTIONS}/photos/{first_id}")
    second = await _upload(client, item_id, make_png())
    old_image = await client.get(f"{ACTIONS}/photos/{first_id}")
    removed = await _post(client, f"/items/{item_id}/photo/remove", {})
    empty_board = (await client.get(FRAGMENT)).text

    assert first.status_code == 200, first.text
    assert f'src="{ACTIONS}/photos/{first_id}"' in board
    assert "Заменить фото" in board and 'data-no-photo="false"' in board
    assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in image.headers["content-security-policy"]
    assert second.status_code == 200 and second.json()["photo_file_id"] != first_id
    assert old_image.status_code == 404
    assert old_image.json()["error"]["code"] == "ERR-ROOMSERVICE-001"
    assert removed.status_code == 200 and removed.json()["photo_file_id"] is None
    assert "нет фото" in empty_board and "Добавить фото" in empty_board
    assert "Без фото <b>1</b>" in empty_board


async def test_other_service_neither_uploads_nor_sees_photo(
    portal_hotel: PortalHotel, client: AsyncClient, fake_file_storage: FileStoragePort
) -> None:
    await _hotel(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.MANAGER, ())
    item_id = await _restaurant_item(client)
    file_id = (await _upload(client, item_id, make_png())).json()["photo_file_id"]
    client.cookies.clear()
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("supply",))

    upload = await _upload(client, item_id, make_png())
    removal = await _post(client, f"/items/{item_id}/photo/remove", {})
    image = await client.get(f"{ACTIONS}/photos/{file_id}")

    for response in (upload, removal, image):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ERR-AUTH-003"


async def test_upload_needs_same_origin_and_multipart(
    portal_hotel: PortalHotel, client: AsyncClient, fake_file_storage: FileStoragePort
) -> None:
    await _hotel(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("fnb",))
    item_id = await _restaurant_item(client)

    no_origin = await _upload(client, item_id, make_png(), headers={})
    foreign = await _upload(client, item_id, make_png(), headers={"origin": "https://evil.test"})
    as_json = await _post(client, f"/items/{item_id}/photo", {"photo": "x"})

    for response in (no_origin, foreign, as_json):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ERR-AUTH-009"


async def test_body_ceiling_file_count_and_content_are_checked(
    portal_hotel: PortalHotel,
    client: AsyncClient,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _hotel(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("fnb",))
    item_id = await _restaurant_item(client)

    two = await client.post(
        f"{ACTIONS}/items/{item_id}/photo",
        files=[
            ("photo", ("a.png", make_png(), "image/png")),
            ("photo", ("b.png", make_png(), "image/png")),
        ],
        headers=SAME_ORIGIN,
    )
    not_photo = await _upload(client, item_id, b"<svg onload=alert(1)/>")
    monkeypatch.setattr(catalog_api, "_PHOTO_BODY_BYTES", 4096)
    too_big = await _upload(client, item_id, make_png((800, 600)) + bytes(8192))
    item = (await client.get(FRAGMENT)).text

    assert two.status_code == 422
    assert not_photo.status_code == 415 and not_photo.json()["error"]["code"] == "ERR-FILES-002"
    assert too_big.status_code == 413 and too_big.json()["error"]["code"] == "ERR-FILES-001"
    assert 'data-no-photo="true"' in item


async def test_only_catalog_page_allows_blob_previews(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    await _hotel(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("fnb",))

    page = await client.get(PAGE)
    home = await client.get(HOME)

    assert page.status_code == 200
    assert "img-src 'self' blob:" in page.headers["content-security-policy"]
    assert 'src="/staff/static/catalog_photo.js' in page.text
    assert "blob:" not in home.headers["content-security-policy"]
