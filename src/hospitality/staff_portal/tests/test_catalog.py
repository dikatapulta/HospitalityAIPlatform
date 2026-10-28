"""Панель «Товары и меню» в кабинете (spec 0038 §6 PR 4, Р-9, Р-10; issue #418).

Право — одна функция `catalog.can_edit_catalog` на меню, страницу и JSON-действия:
без службы каталога — 403 и пункта нет; служба `supply` правит только магазин,
менеджер — оба; JSON без `Origin` — ERR-AUTH-009. Room service выключен — раздела
нет совсем (404). Действия доходят до модуля `room_service`, страница и фрагмент
показывают результат, тексты отеля экранируются; чужой тенант — 404.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    RoomServiceCatalogConfig,
    RoomServiceCatalogKey,
    RoomServiceConfig,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/catalog"
FRAGMENT = f"{PAGE}/fragment"
ACTIONS = f"/staff/{HOTEL_SLUG}/api/catalog"
HOME = f"/staff/{HOTEL_SLUG}"
SAME_ORIGIN = {"origin": "https://test"}
NAV_LINK = f'href="{PAGE}"'

Catalog = room_service_api.RoomServiceCatalog


def _room_service(*, enabled: bool = True) -> RoomServiceConfig:
    catalogs: dict[RoomServiceCatalogKey, RoomServiceCatalogConfig] = {
        "restaurant": RoomServiceCatalogConfig(
            category_key="fnb", service_charge_percent=Decimal("10")
        ),
        "shop": RoomServiceCatalogConfig(
            category_key="supply", service_charge_percent=Decimal("0")
        ),
    }
    return RoomServiceConfig(enabled=enabled, languages=("en", "ru"), catalogs=catalogs)


async def _configure(tenant_id: uuid.UUID, room_service: RoomServiceConfig | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                room_service=room_service,
            ),
        )


async def _hotel(portal_hotel: PortalHotel, *, enabled: bool = True) -> None:
    """Службы отеля и room service: ресторан — `fnb`, магазин — `supply`."""
    with tenant_context(portal_hotel.tenant_id):
        for key, name in (
            (RECEPTION_SERVICE_KEY, "Ресепшен"),
            ("fnb", "Ресторан"),
            ("supply", "Снабжение"),
        ):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
    await _configure(portal_hotel.tenant_id, _room_service(enabled=enabled))


async def _login_as(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole, services: tuple[str, ...]
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=role, service_keys=services
    )
    await submit_login(client, staff_login)


def _names(en: str, ru: str | None = None) -> dict[str, str]:
    return {"en": en, "ru": ru} if ru else {"en": en}


async def _post(client: AsyncClient, path: str, body: object) -> httpx.Response:
    return await client.post(f"{ACTIONS}{path}", json=body, headers=SAME_ORIGIN)


async def _section(client: AsyncClient, catalog: str, en: str, ru: str | None = None) -> str:
    response = await _post(client, f"/catalogs/{catalog}/sections", {"names": _names(en, ru)})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _item(client: AsyncClient, section_id: str, en: str, price_tenge: int) -> str:
    body = {"section_id": section_id, "names": _names(en), "price_minor": price_tenge * 100}
    response = await _post(client, "/items", body)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def test_manager_runs_both_catalogs_end_to_end(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    await _hotel(portal_hotel)
    await submit_login(client, portal_hotel.login)

    breakfast = await _section(client, "restaurant", "Breakfast", "Завтраки")
    drinks = await _section(client, "restaurant", "Drinks", "Напитки")
    syrniki = await _item(client, breakfast, "Syrniki", 2900)
    omelette = await _item(client, breakfast, "Omelette", 2600)
    await _section(client, "shop", "Chargers")

    saved = await _post(
        client,
        f"/items/{omelette}",
        {
            "section_id": drinks,
            "names": {"en": "Omelette", "ru": "Омлет с сыром"},
            "descriptions": {"en": "Three eggs", "ru": "Три яйца"},
            "price_minor": 3100_00,
            "is_available": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert (await _post(client, f"/items/{syrniki}/available", {"available": False})).is_success
    assert (await _post(client, f"/sections/{drinks}/move", {"direction": "up"})).is_success
    accepting = await _post(client, "/catalogs/restaurant/accepting", {"accepting": True})
    assert accepting.json()["accepting_orders"] is True

    page = await client.get(PAGE)
    assert page.status_code == 200
    html = page.text
    assert "Товары и меню" in html and NAV_LINK in html
    assert "/staff/static/signal.js" in html  # страница отеля — с сигналом кабинета
    # Обе вкладки менеджеру; порядок — ресторан, затем магазин (П-13).
    assert html.index(">Ресторан<") < html.index(">Магазин<")
    assert "Заказы в номер: принимаем" in html
    assert "Ведёт служба «Ресторан» · сервисный сбор 10% — настройка отеля" in html
    # Напитки подняты выше завтраков; омлет переехал в напитки с новой ценой.
    assert html.index(">Напитки<") < html.index(">Завтраки<")
    assert "Омлет с сыром" in html and "3 100 ₸" in html
    assert "Закончилось <b>1</b>" in html

    shop = await client.get(PAGE, params={"catalog": "shop"})
    assert "Ведёт служба «Снабжение» · без сервисного сбора" in shop.text
    assert "Chargers" in shop.text and "Завтраки" not in shop.text

    hidden = await _post(client, f"/items/{syrniki}/hidden", {"hidden": True})
    assert hidden.json()["hidden_at"] is not None
    fragment = await client.get(FRAGMENT, params={"catalog": "restaurant"})
    assert fragment.status_code == 200
    assert "Скрыт из меню" in fragment.text and "Скрыто <b>1</b>" in fragment.text
    assert 'class="app' not in fragment.text  # фрагмент — без каркаса


async def test_staff_without_catalog_service_gets_403_and_no_menu_item(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    await _hotel(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.SENIOR, (RECEPTION_SERVICE_KEY,))

    home = await client.get(HOME)
    assert home.status_code == 200 and NAV_LINK not in home.text
    page = await client.get(PAGE)
    assert page.status_code == 403
    assert (await client.get(FRAGMENT)).status_code == 403
    denied = await _post(client, "/catalogs/restaurant/sections", {"names": _names("X")})
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "ERR-AUTH-003")


async def test_supply_service_edits_only_the_shop(
    portal_hotel: PortalHotel, client: AsyncClient, second_client: AsyncClient
) -> None:
    await _hotel(portal_hotel)
    await submit_login(second_client, portal_hotel.login)
    breakfast = await _section(second_client, "restaurant", "Breakfast")
    syrniki = await _item(second_client, breakfast, "Syrniki", 2900)
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("supply",))

    page = await client.get(PAGE)
    assert page.status_code == 200 and NAV_LINK in page.text
    assert ">Магазин<" in page.text and ">Ресторан<" not in page.text
    assert (await client.get(PAGE, params={"catalog": "restaurant"})).status_code == 403

    chargers = await _section(client, "shop", "Chargers")
    assert await _item(client, chargers, "USB-C charger", 6500)
    for path, body in (
        ("/catalogs/restaurant/sections", {"names": _names("X")}),
        ("/catalogs/restaurant/accepting", {"accepting": True}),
        (f"/sections/{breakfast}", {"names": _names("X")}),
        (f"/items/{syrniki}/available", {"available": False}),
        (f"/items/{syrniki}/move", {"direction": "down"}),
        ("/items", {"section_id": breakfast, "names": _names("X"), "price_minor": 100_00}),
    ):
        response = await _post(client, path, body)
        assert (response.status_code, response.json()["error"]["code"]) == (
            403,
            "ERR-AUTH-003",
        ), path


async def test_json_action_without_origin_is_rejected(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    await _hotel(portal_hotel)
    await submit_login(client, portal_hotel.login)
    response = await client.post(
        f"{ACTIONS}/catalogs/restaurant/sections", json={"names": _names("Breakfast")}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (403, "ERR-AUTH-009")
    with tenant_context(portal_hotel.tenant_id):
        catalog = await room_service_api.get_catalog(Catalog.RESTAURANT, include_hidden=True)
    assert catalog.sections == []


@pytest.mark.parametrize("enabled", [False, None], ids=["disabled", "no-room-service"])
async def test_room_service_off_hides_the_section_entirely(
    portal_hotel: PortalHotel, client: AsyncClient, enabled: bool | None
) -> None:
    if enabled is None:
        await _configure(portal_hotel.tenant_id, None)
    else:
        await _hotel(portal_hotel, enabled=enabled)
    await submit_login(client, portal_hotel.login)

    assert NAV_LINK not in (await client.get(HOME)).text
    assert (await client.get(PAGE)).status_code == 404
    response = await _post(client, "/catalogs/restaurant/sections", {"names": _names("X")})
    assert (response.status_code, response.json()["error"]["code"]) == (
        404,
        "ERR-ROOMSERVICE-002",
    )


async def test_hotel_text_is_escaped_on_the_page(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    """Названия ведёт персонал: HTML в них — текст, а не разметка (П-13)."""
    await _hotel(portal_hotel)
    await submit_login(client, portal_hotel.login)
    section = await _section(client, "restaurant", "<script>alert(1)</script>")
    await _item(client, section, "Tea' onmouseover='alert(2)", 1200)

    html = (await client.get(PAGE)).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Tea' onmouseover" not in html  # ни в тексте, ни в data-form


async def test_bad_form_is_rejected_and_nothing_saved(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    await _hotel(portal_hotel)
    await submit_login(client, portal_hotel.login)
    section = await _section(client, "restaurant", "Breakfast")
    for body in (
        {"section_id": section, "names": {"ru": "Сырники"}, "price_minor": 2900_00},
        {"section_id": section, "names": _names("Syrniki"), "price_minor": 2900_50},
        {"section_id": section, "names": _names("Syrniki"), "price_minor": 0},
    ):
        response = await _post(client, "/items", body)
        assert (response.status_code, response.json()["error"]["code"]) == (
            422,
            "ERR-PLATFORM-002",
        )
    with tenant_context(portal_hotel.tenant_id):
        catalog = await room_service_api.get_catalog(Catalog.RESTAURANT, include_hidden=True)
    assert [len(each.items) for each in catalog.sections] == [0]


async def test_other_hotel_item_is_not_found(
    portal_hotel: PortalHotel, client: AsyncClient
) -> None:
    """Изоляция тенантов (P-4): id товара чужого отеля — «не найден», не правка."""
    await _hotel(portal_hotel)
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other Hotel")
        session.add(other)
        await session.flush()
        other_id = other.id
    await _configure(other_id, _room_service())
    with tenant_context(other_id):
        section = await room_service_api.create_section(
            Catalog.RESTAURANT, room_service_api.SectionWrite(names=LocalizedText(en="Theirs"))
        )
        item = await room_service_api.create_item(
            room_service_api.ItemWrite(
                section_id=section.id, names=LocalizedText(en="Their tea"), price_minor=1200_00
            )
        )
    await submit_login(client, portal_hotel.login)

    for path, body in (
        (f"/items/{item.id}/available", {"available": False}),
        (f"/sections/{section.id}", {"names": _names("Mine")}),
    ):
        response = await _post(client, path, body)
        assert (response.status_code, response.json()["error"]["code"]) == (
            404,
            "ERR-ROOMSERVICE-001",
        )
    with tenant_context(other_id):
        theirs = await room_service_api.get_catalog(Catalog.RESTAURANT)
    assert theirs.sections[0].names.en == "Theirs"
    assert theirs.sections[0].items[0].is_available is True
