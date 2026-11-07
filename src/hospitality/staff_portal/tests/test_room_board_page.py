"""Страница и фрагмент доски «Номера» (spec 0061 §4, §7, §9).

Оба выключателя — меню и 404; права доски выездов — 403 и кнопки панели по
уровню и службе; страница и фрагмент рисуют живые проживания и пути, счётчики
и полосы ручного отеля; отметка выезда с доски создаёт путь, и фрагмент его
показывает; чужой тенант не виден. Отель с Opera — `tests/test_room_board_pms.py`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY, RECEPTION_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import room_board
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
ROOMS = f"{HOME}/rooms"
FRAGMENT = f"{ROOMS}/fragment"
ACTIONS = f"{HOME}/api/turnovers"
SAME_ORIGIN = {"origin": "https://test"}
RECEPTION = (RECEPTION_SERVICE_KEY,)
HOUSEKEEPING = (HOUSEKEEPING_SERVICE_KEY,)


def _switches(**values: str) -> Iterator[None]:
    """Свой MonkeyPatch, а не общий `monkeypatch` — довод `_fake_rate_limit` в conftest."""
    patcher = pytest.MonkeyPatch()
    for name, value in values.items():
        patcher.setenv(name, value)
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@pytest.fixture
def board_on() -> Iterator[None]:
    yield from _switches(ENABLE_ROOM_BOARD="true", ENABLE_TURNOVER_BOARD="true")


def _menu_hrefs(html: str) -> list[str]:
    match = re.search(r'<aside class="app-side".*?</aside>', html, re.S)
    assert match is not None, "no side menu"
    return re.findall(r'class="app-nav-item" href="([^"]+)"', match.group(0))


def _cells(html: str) -> dict[str, dict[str, str]]:
    """Клетки доски: номер → его data-атрибуты."""
    cells = {}
    for tag in re.findall(r'<button type="button" class="room" ([^>]*)>', html):
        attrs = dict(re.findall(r'data-([a-z]+)="([^"]*)"', tag))
        cells[attrs["room"]] = attrs
    return cells


def _steps(html: str, status: str) -> list[str]:
    block = re.search(rf'<template data-steps="{status}">(.*?)</template>', html, re.S)
    assert block is not None, status
    return re.findall(r'data-action="([^"]+)"', block.group(1))


def _counters(html: str) -> dict[str, int]:
    found = re.findall(r'data-filter-status="(\w+)"[^>]*>.*?<b>(\d+)</b></button>', html, re.S)
    return {key: int(count) for key, count in found}


async def _hotel(portal_hotel: PortalHotel, rooms: list[dict[str, str]]) -> None:
    """Конфиг со списком номеров, службы и горничная в кабинете."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            portal_hotel.tenant_id,
            TenantConfig.model_validate(
                {
                    "profile": HotelProfile(city="Almaty", country_code="KZ"),
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "hotel_rooms": rooms,
                }
            ),
        )
    with tenant_context(portal_hotel.tenant_id):
        for key, name in (
            (RECEPTION_SERVICE_KEY, "Ресепшен"),
            (HOUSEKEEPING_SERVICE_KEY, "Уборка"),
        ):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
    await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        service_keys=HOUSEKEEPING,
        display_name="Айгуль",
    )


async def _check_in(tenant_id: uuid.UUID, room: str, *, guests: int = 2) -> None:
    with tenant_context(tenant_id):
        await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=room,
                check_out_at=utc_now() + timedelta(days=3),
                guests_count=guests,
            )
        )


async def _login(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole, services: tuple[str, ...]
) -> None:
    login = unique_login()
    await create_staff_user(
        login, tenant_id=portal_hotel.tenant_id, role=role, service_keys=services
    )
    await submit_login(client, login)


async def test_both_switches_are_needed(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await submit_login(client, portal_hotel.login)
    for values in ({}, {"ENABLE_ROOM_BOARD": "true"}, {"ENABLE_TURNOVER_BOARD": "true"}):
        switches = _switches(**values)
        next(switches)
        try:
            page = await client.get(ROOMS)
            fragment = await client.get(FRAGMENT)
            home = await client.get(HOME)
        finally:
            next(switches, None)
        assert page.status_code == 404, values
        assert fragment.status_code == 404, values
        assert ROOMS not in _menu_hrefs(home.text), values


async def test_staff_outside_board_services_gets_403_and_no_menu(
    client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    await _login(client, portal_hotel, StaffRole.STAFF, ())

    page = await client.get(ROOMS)
    fragment = await client.get(FRAGMENT)
    home = await client.get(HOME)

    assert page.status_code == 403
    assert fragment.status_code == 403
    assert ROOMS not in _menu_hrefs(home.text)


async def test_panel_buttons_follow_level_and_service(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    await _hotel(portal_hotel, [{"number": "1401"}])
    await _login(client, portal_hotel, StaffRole.STAFF, HOUSEKEEPING)
    await _login(second_client, portal_hotel, StaffRole.STAFF, RECEPTION)

    maid = (await client.get(ROOMS)).text
    reception = (await second_client.get(ROOMS)).text

    assert ROOMS in _menu_hrefs(maid)
    assert _steps(maid, "occupied") == []
    assert _steps(maid, "reported") == ["inspect", "remark"]
    assert _steps(maid, "cleaned") == []
    assert _steps(reception, "occupied") == ["report"]
    assert _steps(reception, "unknown") == ["report"]
    assert _steps(reception, "reported") == ["cancel"]
    assert ">Отметить выезд</button>" in reception


async def test_no_report_button_without_housekeeping_in_cabinet(
    client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    await submit_login(client, portal_hotel.login)

    page = (await client.get(ROOMS)).text

    assert _steps(page, "occupied") == []
    assert _steps(page, "cleaned") == ["accept", "cancel"]


async def test_board_shows_live_stays_and_turnovers_of_manual_hotel(
    client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    rooms = [{"number": "1401", "room_type": "PRMT"}, {"number": "1402"}, {"number": "1403"}]
    await _hotel(portal_hotel, rooms)
    await _check_in(portal_hotel.tenant_id, "1401", guests=3)
    await _check_in(portal_hotel.tenant_id, "905")
    await submit_login(client, portal_hotel.login)
    report = await client.post(ACTIONS, json={"room_number": "1402"}, headers=SAME_ORIGIN)
    assert report.status_code == 200

    response = await client.get(ROOMS)

    assert response.status_code == 200
    page = response.text
    assert f'href="{ROOMS}" aria-current="page"' in page
    assert "Демонстрация" not in page
    assert room_board.MANUAL_SOURCE_TEXT in page
    assert room_board.UNLISTED_HINT in page
    cells = _cells(page)
    assert cells["1401"]["status"] == "occupied"
    assert cells["1401"]["guests"] == "3"
    assert cells["1401"]["type"] == "PRMT"
    assert cells["1401"]["place"] == "14 этаж"
    assert cells["1402"]["status"] == "reported"
    assert cells["1402"]["turnover"]
    assert cells["1403"]["status"] == "unknown"
    assert cells["905"]["floor"] == room_board.UNLISTED_KEY
    counters = _counters(page)
    assert counters["all"] == 4
    assert counters["occupied"] == 2
    assert counters["unknown"] == 1
    # Без Opera нет ни «свободен», ни «готов» (§3 п. 3–4) — счётчиков-нулей нет.
    assert "vacant" not in counters
    assert "ready" not in counters
    assert "/staff/static/room_board.js?v=" in page
    assert "/staff/static/turnover_actions.js?v=" in page


async def test_report_from_board_shows_up_in_fragment(
    client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    await _hotel(portal_hotel, [{"number": "1403"}])
    await submit_login(client, portal_hotel.login)
    before = await client.get(FRAGMENT)
    assert _cells(before.text)["1403"]["status"] == "unknown"

    report = await client.post(ACTIONS, json={"room_number": "1403"}, headers=SAME_ORIGIN)
    after = await client.get(FRAGMENT)

    assert report.status_code == 200
    assert after.status_code == 200
    assert "<html" not in after.text
    assert _cells(after.text)["1403"]["status"] == "reported"


async def test_fragment_draws_corridor_sides_with_labels(
    client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    """#674: этаж — ряд на сторону коридора с подписью, как на румплане; панель
    номера знает его сторону."""
    rooms = [
        {"number": "1401", "side": "Вид на горы"},
        {"number": "1424", "side": "Вид на город"},
        {"number": "1402", "side": "Вид на горы"},
    ]
    await _hotel(portal_hotel, rooms)
    await submit_login(client, portal_hotel.login)

    fragment = (await client.get(FRAGMENT)).text

    titles = re.findall(r'<h3 class="rooms-side-title">([^<]+)</h3>', fragment)
    assert titles == ["Вид на горы", "Вид на город"]
    cells = _cells(fragment)
    assert list(cells) == ["1401", "1402", "1424"]
    assert cells["1402"]["place"] == "14 этаж · Вид на горы"
    assert cells["1424"]["place"] == "14 этаж · Вид на город"


async def test_empty_list_band_and_other_tenant_not_visible(
    client: AsyncClient, portal_hotel: PortalHotel, board_on: None
) -> None:
    async with platform_session_scope() as session:
        other = Tenant(slug="room-board-other", name="Другой отель")
        session.add(other)
        await session.flush()
        other_id = other.id
    await _check_in(other_id, "1404")
    await _check_in(portal_hotel.tenant_id, "1401")
    await submit_login(client, portal_hotel.login)

    page = (await client.get(ROOMS)).text

    assert room_board.EMPTY_LIST_TEXT in page
    assert set(_cells(page)) == {"1401"}
