"""«Вид гостевого чата» в кабинете и знак отеля у гостя (spec 0066 §4, §6, §8; issue #761).

Только менеджер: остальным — «нет доступа», 403 и пункта меню нет. «Сохранить» пишет
`TenantConfig.guest_chat_look`, страница гостя показывает выбор со следующего открытия.
Знак — файл узла `hotel_logo`: замена и «Убрать» удаляют прежний файл; адрес знака
без сессии отдаёт только текущий знак — любой другой файл тенанта 404.
"""

# ruff: noqa: F811

from __future__ import annotations

import io
import uuid

import pytest
from httpx import AsyncClient, Response
from PIL import Image

from hospitality.platform.config import (
    HotelProfile,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.files import FilePurpose, read_file, store_upload
from hospitality.platform.guest_chat_look import GuestChatLook
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_file_content import make_png
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
PAGE = f"{HOME}/chat-look"
ACTIONS = f"{HOME}/api/chat-look"
CHAT = f"/g/{HOTEL_SLUG}/1507"
SAME_ORIGIN = {"origin": "https://test"}
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


async def _configure(portal_hotel: PortalHotel) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            portal_hotel.tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
            ),
        )


async def _look(portal_hotel: PortalHotel) -> GuestChatLook:
    async with platform_session_scope() as session:
        return (await load_tenant_config(session, portal_hotel.tenant_id)).guest_chat_look


async def _upload(client: AsyncClient, data: bytes, name: str = "logo.png") -> Response:
    mime = "image/svg+xml" if name.endswith(".svg") else "image/png"
    return await client.post(
        f"{ACTIONS}/logo", files={"logo": (name, data, mime)}, headers=SAME_ORIGIN
    )


def _logo(file_id: uuid.UUID | None) -> str:
    return f"/g/{HOTEL_SLUG}/logo/{file_id}"


async def _file_gone(portal_hotel: PortalHotel, file_id: uuid.UUID) -> bool:
    with tenant_context(portal_hotel.tenant_id):
        try:
            await read_file(file_id)
        except AppError as error:
            return error.code == "ERR-FILES-004"
    return False


async def test_manager_page_and_preview(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await _configure(portal_hotel)
    await submit_login(client, portal_hotel.login)

    page = await client.get(PAGE)
    preview = await client.get(
        f"{ACTIONS}/preview", params={"palette": "emerald", "pattern": "off", "theme": "night"}
    )
    unknown = await client.get(
        f"{ACTIONS}/preview", params={"palette": "pink", "pattern": "off", "theme": "day"}
    )

    assert page.status_code == 200, page.text
    assert 'value="sky_gold" checked>Небо и золото' in page.text
    assert 'value="normal" checked>Обычный' in page.text
    assert "palette=sky_gold&amp;pattern=normal&amp;theme=night" in page.text
    assert preview.status_code == 200, preview.text
    assert preview.headers["x-frame-options"] == "SAMEORIGIN"
    assert "default-src 'none'" in preview.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in preview.headers["content-security-policy"]
    assert "<script" not in preview.text
    assert "@media all{:root{--wall-base:#0b1a16;" in preview.text
    assert ".wall img{display:none}" in preview.text
    assert unknown.status_code == 422


@pytest.mark.parametrize("role", [StaffRole.STAFF, StaffRole.SENIOR])
async def test_not_manager_has_no_page_and_no_actions(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole
) -> None:
    await _configure(portal_hotel)
    login = unique_login()
    await create_staff_user(
        login, tenant_id=portal_hotel.tenant_id, role=role, service_keys=(RECEPTION_SERVICE_KEY,)
    )
    await submit_login(client, login)

    home = (await client.get(HOME)).text
    page = await client.get(PAGE)
    save = await client.post(
        ACTIONS, json={"palette": "sunset", "pattern": "normal"}, headers=SAME_ORIGIN
    )
    preview = await client.get(
        f"{ACTIONS}/preview", params={"palette": "sunset", "pattern": "normal", "theme": "day"}
    )

    assert f'href="{PAGE}"' not in home
    assert page.status_code == 403
    assert save.status_code == 403 and preview.status_code == 403
    assert await _look(portal_hotel) == GuestChatLook()


async def test_save_changes_guest_chat(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await _configure(portal_hotel)
    await submit_login(client, portal_hotel.login)
    before = (await client.get(CHAT)).text

    saved = await client.post(
        ACTIONS, json={"palette": "sunset", "pattern": "bright"}, headers=SAME_ORIGIN
    )
    after = (await client.get(CHAT)).text
    page = (await client.get(PAGE)).text
    unknown = await client.post(
        ACTIONS, json={"palette": "pink", "pattern": "normal"}, headers=SAME_ORIGIN
    )
    no_origin = await client.post(ACTIONS, json={"palette": "emerald", "pattern": "normal"})

    assert saved.status_code == 200, saved.text
    assert await _look(portal_hotel) == GuestChatLook(palette="sunset", pattern="bright")
    assert "--wall-base:#f5e3dc" not in before
    assert "--wall-base:#f5e3dc;" in after and ".wall img.on{opacity:.9}" in after
    assert 'value="sunset" checked' in page and 'value="bright" checked' in page
    assert unknown.status_code == 422
    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "ERR-AUTH-009"
    assert (await _look(portal_hotel)).palette == "sunset"


async def test_logo_set_replace_remove(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _configure(portal_hotel)
    await submit_login(client, portal_hotel.login)

    first = await _upload(client, make_png((900, 600)))
    first_id = (await _look(portal_hotel)).logo_file_id
    chat = (await client.get(CHAT)).text
    bind = (await client.get(f"/w/{HOTEL_SLUG}/b/any-token")).text
    image = await client.get(_logo(first_id))
    second = await _upload(client, make_png())
    second_id = (await _look(portal_hotel)).logo_file_id
    old_image = await client.get(_logo(first_id))
    removed = await client.post(f"{ACTIONS}/logo/remove", json={}, headers=SAME_ORIGIN)
    gone = await client.get(_logo(second_id))
    plain_chat = (await client.get(CHAT)).text

    assert first.status_code == 200, first.text
    assert first_id is not None
    assert f'<img src="{_logo(first_id)}" alt="">' in chat
    assert f'<link rel="icon" href="{_logo(first_id)}">' in chat
    assert f'<img src="{_logo(first_id)}" alt="">' in bind
    assert image.status_code == 200 and image.headers["content-type"] == "image/webp"
    assert max(Image.open(io.BytesIO(image.content)).size) == 256
    assert second.status_code == 200 and second_id not in (None, first_id)
    assert old_image.status_code == 404
    assert old_image.json()["error"]["code"] == "ERR-FILES-004"
    assert await _file_gone(portal_hotel, first_id)
    assert removed.status_code == 200
    assert (await _look(portal_hotel)).logo_file_id is None
    assert gone.status_code == 404
    assert second_id is not None and await _file_gone(portal_hotel, second_id)
    assert "/logo/" not in plain_chat


async def test_logo_address_serves_only_current_logo(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    """Адрес без сессии: фото к заявке того же тенанта по нему не отдаётся."""
    await _configure(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        other = await store_upload(FilePurpose.REQUEST_PHOTO, make_png(), "room.png")

    foreign = await client.get(_logo(other.id))
    unknown_hotel = await client.get(f"/g/no-such-hotel/logo/{other.id}")

    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "ERR-FILES-004"
    assert unknown_hotel.status_code == 404
    assert unknown_hotel.json()["error"]["code"] == "ERR-WEB-001"


async def test_svg_logo_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _configure(portal_hotel)
    await submit_login(client, portal_hotel.login)

    response = await _upload(client, _SVG, "logo.svg")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "ERR-FILES-002"
    assert (await _look(portal_hotel)).logo_file_id is None
