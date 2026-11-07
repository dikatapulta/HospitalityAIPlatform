"""Страница витрины для посетителей (spec 0069 §2.2, §4, §7 п. 3 и 7; ADR-025).

Режимы: по коду — экран кода без cookie, сцена и подбор — 403 `005`; верный код —
cookie и 200; смена кода гасит прежнюю cookie; публичный — без cookie 200;
кабинетный (без кода и флага), выключенный, без сцены, чужой slug — 404 `001`.
Лимиты ввода кода по адресу и по отелю — 429 `003`. Страница: CSP `'self'`, ничего
извне, единственная cookie — доступ; `price_note` — только когда задан.
"""

from __future__ import annotations

import re

from httpx import AsyncClient, Response

from hospitality.channels.showroom import access, assets
from hospitality.channels.showroom.tests.conftest import (
    CODE,
    SLUG,
    ShowroomHotel,
    create_showroom_hotel,
    set_showroom,
)
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom.tests.conftest import create_hotel
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis


def _code(response: Response) -> str:
    return str(response.json()["error"]["code"])


async def _enter(client: AsyncClient, code: str = CODE, slug: str = SLUG) -> Response:
    return await client.post(f"/showroom/{slug}/access", json={"code": code})


async def test_without_cookie_page_shows_code_screen_and_data_is_closed(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    page = await client.get(f"/showroom/{SLUG}?lang=ru")
    assert page.status_code == 200
    assert "Введите код доступа" in page.text
    assert "data-sr-root" not in page.text
    scene = await client.get(f"/showroom/{SLUG}/scene")
    rooms = await client.get(f"/showroom/{SLUG}/rooms")
    assert (scene.status_code, _code(scene)) == (403, "ERR-SHOWROOM-005")
    assert (rooms.status_code, _code(rooms)) == (403, "ERR-SHOWROOM-005")


async def test_right_code_sets_cookie_and_opens_scene(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    entered = await _enter(client)
    assert entered.status_code == 204
    cookie = entered.headers["set-cookie"]
    assert cookie.startswith(f"{access.COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", f"Path=/showroom/{SLUG}"):
        assert attribute.lower() in cookie.lower()
    assert "Max-Age=604800" in cookie
    assert CODE not in cookie

    page = await client.get(f"/showroom/{SLUG}?lang=en")
    assert "data-sr-root" in page.text
    scene = await client.get(f"/showroom/{SLUG}/scene")
    assert scene.status_code == 200
    assert scene.json()["scene"]["building"]["floors_total"] == 16
    rooms = await client.get(f"/showroom/{SLUG}/rooms", params={"floor_from": 15, "side": "south"})
    assert [room["floor"] for room in rooms.json()["rooms"]] == [15] * 10


async def test_wrong_code_is_403_without_reason(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    rejected = await _enter(client, code="not-the-code")
    assert (rejected.status_code, _code(rejected)) == (403, "ERR-SHOWROOM-005")
    assert "set-cookie" not in rejected.headers


async def test_new_code_kills_old_cookie(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)
    assert (await client.get(f"/showroom/{SLUG}/scene")).status_code == 200
    with tenant_context(showroom_hotel.tenant_id):
        await showroom_api.set_access_code("brand-new-code")
    stale = await client.get(f"/showroom/{SLUG}/scene")
    assert (stale.status_code, _code(stale)) == (403, "ERR-SHOWROOM-005")


async def test_forged_cookie_is_refused(client: AsyncClient, showroom_hotel: ShowroomHotel) -> None:
    client.cookies.set(access.COOKIE, "0" * 64, domain="test", path=f"/showroom/{SLUG}")
    assert (await client.get(f"/showroom/{SLUG}/scene")).status_code == 403


async def test_public_mode_opens_without_cookie(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await set_showroom(showroom_hotel.tenant_id, public_enabled=True)
    assert "data-sr-root" in (await client.get(f"/showroom/{SLUG}")).text
    assert (await client.get(f"/showroom/{SLUG}/scene")).status_code == 200


async def test_cabinet_only_disabled_no_scene_and_unknown_slug_are_404(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    cabinet_only = ShowroomConfig(enabled=True, sales_category_key="sales")
    await create_showroom_hotel("cabinet-only", showroom=cabinet_only, code=None)
    await create_showroom_hotel(
        "disabled", showroom=cabinet_only.model_copy(update={"enabled": False})
    )
    no_scene = await create_hotel("no-scene")
    with tenant_context(no_scene):
        await showroom_api.set_access_code(CODE)
    for slug in ("cabinet-only", "no-scene", "disabled", "nobody"):
        for path in (f"/showroom/{slug}", f"/showroom/{slug}/scene"):
            response = await client.get(path)
            assert (response.status_code, _code(response)) == (404, "ERR-SHOWROOM-001"), path
        assert (await _enter(client, slug=slug)).status_code == 404


async def test_code_entry_is_limited_by_address(
    client: AsyncClient, showroom_hotel: ShowroomHotel, limits: FakeRateLimitRedis
) -> None:
    for _ in range(access.CODE_IP_LIMIT):
        assert (await _enter(client, code="wrong-code-x")).status_code == 403
    limited = await _enter(client)
    assert (limited.status_code, _code(limited)) == (429, "ERR-SHOWROOM-003")


async def test_code_entry_is_limited_by_hotel(
    client: AsyncClient, showroom_hotel: ShowroomHotel, limits: FakeRateLimitRedis
) -> None:
    await _enter(client, code="wrong-code-x")
    tenant_key = next(k for k in limits.counters if "showroom_code_verify_tenant" in k)
    limits.counters[tenant_key] = access.CODE_TENANT_LIMIT
    limited = await _enter(client)
    assert (limited.status_code, _code(limited)) == (429, "ERR-SHOWROOM-003")


async def test_page_loads_nothing_from_outside_and_sets_no_other_cookies(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)
    page = await client.get(f"/showroom/{SLUG}")
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    assert "set-cookie" not in page.headers
    sources = re.findall(r'(?:src|href)="([^"]+)"', page.text)
    assert sources and all(source.startswith("/") for source in sources)
    for source in (s for s in sources if s.startswith("/showroom/static/")):
        asset = await client.get(source)
        assert asset.status_code == 200, source
        assert "immutable" in asset.headers["cache-control"]
    assert (await client.get("/showroom/static/viewer.js")).headers["cache-control"] == "no-cache"
    assert (await client.get("/showroom/static/evil.js")).status_code == 404


def test_viewer_modules_import_only_local_registered_files() -> None:
    """Модули просмотрщика (#816) грузят друг друга относительным адресом: каждый импорт —
    свой файл статики, иначе 404 в браузере, а внешний адрес CSP не пустит."""
    for name, asset in assets.ASSETS.items():
        if not name.startswith("viewer"):
            continue
        imports = re.findall(r'from\s+"([^"]+)"', asset.content.decode())
        assert imports, name
        for target in imports:
            assert target.startswith("./"), (name, target)
            assert target[2:] in assets.ASSETS, (name, target)


async def test_page_language_follows_query_and_accept_language(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)
    kk = await client.get(f"/showroom/{SLUG}", headers={"Accept-Language": "kk-KZ,ru;q=0.8"})
    assert '<html lang="kk">' in kk.text
    assert "қонақүйінің 3D-витринасы" in kk.text
    en = await client.get(f"/showroom/{SLUG}")
    assert '<html lang="en">' in en.text
    ru = await client.get(f"/showroom/{SLUG}?lang=ru", headers={"Accept-Language": "kk"})
    assert "3D-витрина отеля Hotel showroom-hotel" in ru.text


async def test_price_note_only_when_hotel_set_it(
    client: AsyncClient, showroom_hotel: ShowroomHotel
) -> None:
    await _enter(client)
    types = (await client.get(f"/showroom/{SLUG}/scene")).json()["scene"]["room_types"]
    assert types["standard"]["price_note"]["ru"] == "от 40 000 ₸ за ночь"
    assert types["deluxe"]["price_note"] is None
