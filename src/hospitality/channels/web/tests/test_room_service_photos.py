"""Фото товара на странице гостя (spec 0038 §6 п. 7, PR 7): адрес — id файла из
данных каталога, выдача — за сессией гостя, скрытый товар и чужой id — 404,
хранилище потеряло файл — 503 (страница рисует плашку с буквой)."""

# ruff: noqa: F811

from __future__ import annotations

import uuid

from httpx import AsyncClient

from hospitality.channels.web.tests.test_room_service import (  # noqa: F401  (фикстуры)
    BASE,
    Menu,
    _enter,
    client,
    menu,
)
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.files import UploadedFile
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_file_content import make_png


async def _photo(menu: Menu, item_id: uuid.UUID) -> uuid.UUID:
    async with platform_session_scope() as session:
        user = User(display_name="Айгерим")
        session.add(user)
        await session.flush()
        user_id = user.id
    with tenant_context(menu.hotel.tenant_id):
        item = await room_service_api.set_item_photo(
            item_id, make_png(), "lagman.png", uploaded_by_user_id=user_id
        )
    assert item.photo_file_id is not None
    return item.photo_file_id


async def test_guest_gets_photo_of_visible_item_only(
    client: AsyncClient, menu: Menu, fake_file_storage: FileStoragePort
) -> None:
    lagman = await _photo(menu, menu.lagman)
    tea = await _photo(menu, menu.tea)
    with tenant_context(menu.hotel.tenant_id):
        await room_service_api.set_hidden(menu.tea, True)

    anonymous = await client.get(f"{BASE}/menu/photos/{lagman}")
    await _enter(client, menu.hotel.access_code)
    data = (await client.get(f"{BASE}/menu/data")).json()
    image = await client.get(f"{BASE}/menu/photos/{lagman}")
    hidden = await client.get(f"{BASE}/menu/photos/{tea}")
    unknown = await client.get(f"{BASE}/menu/photos/{uuid.uuid4()}")

    items = {item["id"]: item for item in data["catalogs"][0]["sections"][0]["items"]}
    assert items[str(menu.lagman)]["photo_file_id"] == str(lagman)
    assert anonymous.status_code == 401
    assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
    assert image.headers["x-content-type-options"] == "nosniff"
    for response in (hidden, unknown):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == room_service_api.ERR_ROOMSERVICE_NOT_FOUND


async def test_lost_file_is_503_for_placeholder(
    client: AsyncClient, menu: Menu, fake_file_storage: FileStoragePort
) -> None:
    file_id = await _photo(menu, menu.lagman)
    with tenant_context(menu.hotel.tenant_id):
        async with session_scope() as session:
            row = await session.get(UploadedFile, file_id)
    assert row is not None
    await fake_file_storage.delete(row.storage_key)
    await _enter(client, menu.hotel.access_code)

    response = await client.get(f"{BASE}/menu/photos/{file_id}")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ERR-FILES-006"
