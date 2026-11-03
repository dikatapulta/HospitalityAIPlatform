"""Чат как приложение с экрана «Домой» и вход «комната + код» (spec 0068, #783)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.web import homescreen, page
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, ROOM, WebHotel
from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context

BASE = f"/g/{HOTEL_SLUG}/{ROOM}"


@pytest.fixture
async def client(web_hotel: WebHotel) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as http:
        yield http


async def test_manifest_names_own_hotel_and_opens_own_room(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == manifest["short_name"] == "Demo Hotel"
    assert manifest["start_url"] == BASE
    assert manifest["scope"] == manifest["id"] == f"/g/{HOTEL_SLUG}/"
    assert manifest["display"] == "standalone"
    sizes = {(icon["sizes"], icon.get("purpose", "any")) for icon in manifest["icons"]}
    assert sizes == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}


async def test_manifest_of_unknown_hotel_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/g/no-such-hotel/{ROOM}/manifest.webmanifest")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-WEB-001"


async def test_manifest_start_url_keeps_room_encoded(client: AsyncClient) -> None:
    response = await client.get(f"/g/{HOTEL_SLUG}/A%2012/manifest.webmanifest")

    assert response.json()["start_url"] == f"/g/{HOTEL_SLUG}/A%2012"


@pytest.mark.parametrize("name", sorted(homescreen.ICONS))
async def test_icons_are_png_cached_forever(name: str) -> None:
    """Иконки — своим маршрутом, а не страницей чата отеля со slug «static»."""
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as http:
        response = await http.get(homescreen.icon_url(name))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.content == homescreen.ICONS[name]
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_page_links_manifest_of_its_room_and_prefills_room(client: AsyncClient) -> None:
    body = (await client.get(BASE)).text

    assert f'<link rel="manifest" href="{BASE}/manifest.webmanifest">' in body
    touch_icon = homescreen.icon_url("chat-icon-180.png")
    assert f'<link rel="apple-touch-icon" href="{touch_icon}">' in body
    assert '<meta name="apple-mobile-web-app-title" content="Demo Hotel">' in body
    assert f'id="room" value="{ROOM}"' in body
    assert 'id="scroller"' in body


def test_room_from_address_is_escaped_and_cannot_reach_markers() -> None:
    """Комнату в адресе задаёт кто угодно: экранирована и не подставляется вместо маркеров."""
    html = page.render(tenant_slug=HOTEL_SLUG, room_number='"><script>x</script>')
    assert '"><script>x</script>' not in html
    assert 'value="&quot;&gt;&lt;script&gt;x&lt;/script&gt;"' in html

    markers = page.render(tenant_slug=HOTEL_SLUG, room_number="__JS__")
    assert 'value="__JS__"' in markers and "/__JS__/manifest.webmanifest" in markers


def test_bind_page_has_no_manifest() -> None:
    """Страница листка лежит вне `scope` приложения и сразу уводит в чат."""
    html = page.render_bind(hotel_name="Demo Hotel")
    assert 'rel="manifest"' not in html and "__APP__" not in html and 'id="room"' not in html


async def test_corrected_room_with_its_code_opens_session_of_that_room(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    """Иконка осталась от 101, гостя переселили в 202: исправил комнату — вход в 202, и
    `GET …/messages` с адреса 101 говорит странице комнату сессии."""
    with tenant_context(web_hotel.tenant_id):
        moved = await check_in(
            StayCheckIn(room_number="202", check_out_at=utc_now().replace(year=utc_now().year + 1))
        )

    response = await client.post(f"/g/{HOTEL_SLUG}/202/session", json={"code": moved.access_code})
    assert response.status_code == 200
    assert response.json()["room_number"] == "202"

    history = await client.get(f"{BASE}/messages")
    assert history.status_code == 200
    assert history.json()["room_number"] == "202"


async def test_wrong_room_with_valid_code_is_403_without_reason(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    """Код комнаты 101 у комнаты 202 — тот же 403, что и неверный код: причина не уточняется."""
    wrong_room = await client.post(
        f"/g/{HOTEL_SLUG}/202/session", json={"code": web_hotel.access_code}
    )
    wrong_code = await client.post(f"{BASE}/session", json={"code": "000-000"})

    assert wrong_room.status_code == wrong_code.status_code == 403
    room_error, code_error = wrong_room.json()["error"], wrong_code.json()["error"]
    assert room_error["code"] == code_error["code"] == "ERR-WEB-003"
    assert room_error["message"] == code_error["message"]
