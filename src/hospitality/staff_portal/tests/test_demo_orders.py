"""Демо «Заказы в номер» в кабинете (issue #438): выключатель, вход, карточки.

DoD issue: `ENABLE_DEMO_ORDERS=false` — страницы нет (404) и карточки на главной
нет; `true` без сессии — на вход этого отеля (канон `_page_context`); `true` с
сессией — 200 с заказами обеих служб и пометкой «Демо».
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
ORDERS = f"{HOME}/orders"


@pytest.fixture
def demo_orders_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Включённый выключатель на один тест — канон `service_requests_enabled`
    (`ai/tests/conftest.py`): setenv + сброс `lru_cache` настроек до и после."""
    monkeypatch.setenv("ENABLE_DEMO_ORDERS", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_switch_off_means_no_page(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Умолчание — выключено: 404 и без сессии (не редирект на вход — снаружи
    демо не видно), и с сессией; карточки на главной нет."""
    anonymous = await client.get(ORDERS)
    await submit_login(client, portal_hotel.login)
    signed_in = await client.get(ORDERS)
    home = await client.get(HOME)

    assert anonymous.status_code == 404
    assert signed_in.status_code == 404
    assert "Заказы в номер" not in home.text


async def test_switch_on_without_session_leads_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel, demo_orders_enabled: None
) -> None:
    response = await client.get(ORDERS)

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/login"


async def test_switch_on_with_session_renders_both_services(
    client: AsyncClient, portal_hotel: PortalHotel, demo_orders_enabled: None
) -> None:
    await submit_login(client, portal_hotel.login)

    response = await client.get(ORDERS)
    home = await client.get(HOME)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert "Демо на выдуманных данных" in page
    assert "Служба: F&amp;B" in page
    assert "Служба: Снабжение" in page
    # Карточка по формату spec 0038 §8.3; сумма — из общего демо-каталога.
    assert "🍽 Заказ #12 · № 512 · 4 800 ₸" in page
    assert "Лагман ×1 — 3 400 ₸" in page
    assert "Затем пробейте заказ в кассе ресторана" in page
    assert "/staff/static/demo_orders.js?v=" in page
    assert f'href="{ORDERS}"' in home.text


async def test_any_role_sees_demo(
    client: AsyncClient, portal_hotel: PortalHotel, demo_orders_enabled: None
) -> None:
    """Заказы ведут F&B и снабжение — рядовой сотрудник, не менеджер."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await submit_login(client, staff_login)

    response = await client.get(ORDERS)

    assert response.status_code == 200


async def test_demo_script_is_served(client: AsyncClient) -> None:
    response = await client.get("/staff/static/demo_orders.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
