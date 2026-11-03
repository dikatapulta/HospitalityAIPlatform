"""Раздел «3D-витрина» кабинета (spec 0069 §2.2, §4 «Кабинет», §7 п. 3 и 7; issue #803).

Видят менеджер и службы продаж и бронирования; чужая служба — «нет доступа» и 403
`ERR-AUTH-003`; витрина выключена — 404 и пункта меню нет. Загрузка сцены — менеджер,
CSRF-щит, отказ схемы — 422 `ERR-SHOWROOM-002` с путём, прежняя сцена остаётся,
строка журнала. Cookie посетителя — менеджеру, только когда страница видна снаружи.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.channels.showroom import access
from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffAction, StaffActionLog, StaffRole
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.tools.demo_showroom import build_demo_scene
from tests.test_staff_auth import create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/showroom"
SAME_ORIGIN = {"origin": "https://test"}
DEMO_3D = "https://3d.example.com/hotel"


async def _configure(hotel: PortalHotel, *, enabled: bool = True, code: str | None = None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            hotel.tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                showroom=ShowroomConfig(enabled=enabled, sales_category_key="sales"),
            ),
        )
    with tenant_context(hotel.tenant_id):
        for key in ("sales", "housekeeping"):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=key)
            )
        await showroom_api.replace_scene(build_demo_scene(), acting_user=None)
        if code is not None:
            await showroom_api.set_access_code(code)


async def _login(client: AsyncClient, hotel: PortalHotel, *services: str) -> None:
    if not services:
        response = await submit_login(client, hotel.login)
    else:
        staff_login = unique_login()
        await create_staff_user(
            staff_login, tenant_id=hotel.tenant_id, role=StaffRole.STAFF, service_keys=services
        )
        response = await submit_login(client, staff_login)
    assert response.status_code == 303


@pytest.fixture
def demo_3d_link() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DEMO_3D_URL", DEMO_3D)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def test_manager_sees_section_instead_of_demo_link(
    client: AsyncClient, portal_hotel: PortalHotel, demo_3d_link: None
) -> None:
    await _configure(portal_hotel)
    await _login(client, portal_hotel)
    page = await client.get(PAGE)
    assert page.status_code == 200
    assert f'href="{PAGE}"' in page.text
    assert DEMO_3D not in page.text
    assert "data-sr-root" in page.text
    assert "Загрузить сцену (JSON)" in page.text
    assert "Сцена обновлена: версия 1" in page.text
    # Кабинетный режим: кода нет — кнопки страницы посетителя нет.
    assert "Открыть страницу посетителя" not in page.text
    scene = await client.get(f"{PAGE}/scene")
    assert scene.json()["scene"]["building"]["floors_total"] == 16
    rooms = await client.get(f"{PAGE}/rooms", params={"floor_from": 15, "count": 3})
    assert [room["number"] for room in rooms.json()["rooms"]] == ["1501", "1502", "1503"]


async def test_sales_service_sees_section_without_manager_tools(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _configure(portal_hotel)
    await _login(client, portal_hotel, "sales")
    page = await client.get(PAGE)
    assert page.status_code == 200
    assert f'href="{PAGE}"' in page.text
    assert "Загрузить сцену" not in page.text
    upload = await client.post(f"{PAGE}/scene", json={"text": "{}"}, headers=SAME_ORIGIN)
    assert upload.status_code == 403


async def test_other_service_is_refused_and_has_no_menu_item(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _configure(portal_hotel)
    await _login(client, portal_hotel, "housekeeping")
    assert f'href="{PAGE}"' not in (await client.get(f"/staff/{HOTEL_SLUG}/requests")).text
    assert (await client.get(PAGE)).status_code == 403
    scene = await client.get(f"{PAGE}/scene")
    assert (scene.status_code, scene.json()["error"]["code"]) == (403, "ERR-AUTH-003")


async def test_disabled_section_is_404_without_menu_item(
    client: AsyncClient, portal_hotel: PortalHotel, demo_3d_link: None
) -> None:
    await _configure(portal_hotel, enabled=False)
    await _login(client, portal_hotel)
    queue = await client.get(f"/staff/{HOTEL_SLUG}/requests")
    assert f'href="{PAGE}"' not in queue.text
    assert DEMO_3D in queue.text
    assert (await client.get(PAGE)).status_code == 404
    assert (await client.get(f"{PAGE}/scene")).status_code == 404


async def test_manager_uploads_scene_and_bad_scene_keeps_the_old_one(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _configure(portal_hotel)
    await _login(client, portal_hotel)
    document = build_demo_scene().model_dump(mode="json")
    saved = await client.post(
        f"{PAGE}/scene", json={"text": json.dumps(document)}, headers=SAME_ORIGIN
    )
    assert saved.status_code == 200
    assert saved.json()["scene_version"] == 2
    assert saved.json()["message"].startswith("Сцена обновлена: версия 2, ")

    document["floors"][3]["rooms"][0]["type_key"] = "lux"
    rejected = await client.post(
        f"{PAGE}/scene", json={"text": json.dumps(document)}, headers=SAME_ORIGIN
    )
    assert rejected.status_code == 422
    error = rejected.json()["error"]
    assert error["code"] == "ERR-SHOWROOM-002"
    assert error["message"].startswith("floors.3.rooms.0.type_key — ")

    no_origin = await client.post(f"{PAGE}/scene", json={"text": "{}"})
    assert (no_origin.status_code, no_origin.json()["error"]["code"]) == (403, "ERR-AUTH-009")

    with tenant_context(portal_hotel.tenant_id):
        current = await showroom_api.get_scene()
        async with session_scope() as session:
            journal = (await session.scalars(select(StaffActionLog.action))).all()
    assert current is not None and current.scene_version == 2
    assert list(journal) == [StaffAction.SHOWROOM_SCENE_REPLACED]


async def test_manager_gets_visitor_cookie_only_when_page_is_open(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _configure(portal_hotel)
    await _login(client, portal_hotel)
    closed = await client.post(f"{PAGE}/access-cookie", json={}, headers=SAME_ORIGIN)
    assert (closed.status_code, closed.json()["error"]["code"]) == (404, "ERR-SHOWROOM-001")

    with tenant_context(portal_hotel.tenant_id):
        await showroom_api.set_access_code("show-2026-code")
    assert "Открыть страницу посетителя" in (await client.get(PAGE)).text
    granted = await client.post(f"{PAGE}/access-cookie", json={}, headers=SAME_ORIGIN)
    assert granted.status_code == 204
    assert f"Path=/showroom/{HOTEL_SLUG}" in granted.headers["set-cookie"]
    assert granted.headers["set-cookie"].startswith(f"{access.COOKIE}=")
    visitor = await client.get(f"/showroom/{HOTEL_SLUG}/scene")
    assert visitor.status_code == 200


async def test_sales_service_asks_the_assistant_in_the_cabinet(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Помощник кабинета (spec 0069 §2.3): тот же ход, что у посетителя, без кода доступа."""
    await _configure(portal_hotel)
    await _login(client, portal_hotel, "sales")
    page = await client.get(PAGE)
    assert f'"navigateUrl": "{PAGE}/navigate"' in page.text
    assert "/showroom/static/assistant.js?v=" in page.text

    answer = await client.post(
        f"{PAGE}/navigate", json={"text": "этаж 15", "language": "ru"}, headers=SAME_ORIGIN
    )

    assert answer.status_code == 200
    assert answer.json()["commands"] == [{"type": "focus_floor", "floor": 15}]


async def test_assistant_in_the_cabinet_keeps_service_boundary_and_csrf_shield(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _configure(portal_hotel)
    await _login(client, portal_hotel, "housekeeping")
    body = {"text": "этаж 15", "language": "ru"}

    foreign = await client.post(f"{PAGE}/navigate", json=body, headers=SAME_ORIGIN)
    no_origin = await client.post(f"{PAGE}/navigate", json=body)

    assert (foreign.status_code, foreign.json()["error"]["code"]) == (403, "ERR-AUTH-003")
    assert (no_origin.status_code, no_origin.json()["error"]["code"]) == (403, "ERR-AUTH-009")
