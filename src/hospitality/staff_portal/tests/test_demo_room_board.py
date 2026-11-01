"""Демо «Номера» в кабинете (issue #603): выключатель, меню, доска и шаги.

DoD issue: `ENABLE_DEMO_ROOM_BOARD=false` — страницы нет (404) и пункта меню
нет; `true` — 200 и пункт «Номера» любому вошедшему сотруднику. Данные доски:
этажи 13–25 по 12 номеров, номера уникальны, все статусы есть. Шаги — по
таблице `demo_room_board.STEPS`, которую шаблон отдаёт кнопками с `data-to`
(JS своих переходов не знает, поэтому проверяется Python-описание и разметка).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.staff_portal import demo_room_board
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
ROOMS = f"{HOME}/rooms"
# Время показа — фиксированное: строки «выезд 10:40» не зависят от часа прогона (#574).
NOW = datetime(2026, 11, 5, 11, 0, tzinfo=UTC)


def _switches(monkeypatch: pytest.MonkeyPatch, **values: str) -> Iterator[None]:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def room_board_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Включённый выключатель на один тест — канон `test_demo_handover`."""
    yield from _switches(monkeypatch, ENABLE_DEMO_ROOM_BOARD="true")


@pytest.fixture
def room_board_with_lost_found(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _switches(monkeypatch, ENABLE_DEMO_ROOM_BOARD="true", ENABLE_DEMO_LOST_FOUND="true")


def _menu_hrefs(html: str) -> list[str]:
    match = re.search(r'<aside class="app-side".*?</aside>', html, re.S)
    assert match is not None, "no side menu"
    return re.findall(r'class="app-nav-item" href="([^"]+)"', match.group(0))


def _staff() -> StaffContext:
    return cast(StaffContext, SimpleNamespace(tenant_name="Отель", tenant_slug=HOTEL_SLUG))


async def test_switch_off_means_no_page(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Умолчание — выключено: 404 и без сессии, и с сессией; пункта меню нет."""
    anonymous = await client.get(ROOMS)
    await submit_login(client, portal_hotel.login)
    signed_in = await client.get(ROOMS)
    home = await client.get(HOME)

    assert anonymous.status_code == 404
    assert signed_in.status_code == 404
    assert ROOMS not in _menu_hrefs(home.text)


async def test_switch_on_without_session_leads_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel, room_board_enabled: None
) -> None:
    response = await client.get(ROOMS)

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/login"


async def test_any_staff_member_sees_board_and_menu_item(
    client: AsyncClient, portal_hotel: PortalHotel, room_board_enabled: None
) -> None:
    """Рядовой сотрудник без служб — тоже: в демо нет данных, которые надо прятать."""
    login = unique_login()
    await create_staff_user(login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await submit_login(client, login)

    response = await client.get(ROOMS)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert ROOMS in _menu_hrefs(page)
    assert f'href="{ROOMS}" aria-current="page"' in page
    assert (
        "Демонстрация: выдуманные номера. Настоящая доска появится после подключения Opera" in page
    )
    assert page.count('class="room"') == 13 * 12
    assert 'data-room="1301"' in page and 'data-room="2512"' in page
    assert 'data-room="1201"' not in page and 'data-room="2601"' not in page
    assert "/staff/static/demo_room_board.js?v=" in page
    assert "/staff/static/demo_room_board.css?v=" in page
    # Без демо «Забытые вещи» кнопки «Найдена вещь» нет.
    assert "Найдена вещь" not in page


async def test_step_buttons_are_rendered_from_the_table(
    client: AsyncClient, portal_hotel: PortalHotel, room_board_enabled: None
) -> None:
    """Каждый шаг таблицы — кнопкой с `data-to` в шаблоне своего статуса;
    надписи — как у живой доски выездов (`turnovers._row`)."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(ROOMS)).text

    for status, steps in demo_room_board.STEPS.items():
        block = re.search(rf'<template data-steps="{status}">(.*?)</template>', page, re.S)
        assert block is not None, status
        buttons = re.findall(r'data-step="([^"]+)" data-to="([^"]+)"', block.group(1))
        assert buttons == [(step.action, step.to) for step in steps], status
    for label in ("Отметить выезд", "Проверено", "Замечание", "Убираю", "Убрано", "Принять"):
        assert f">{label}</button>" in page


async def test_lost_found_button_carries_its_page_when_that_demo_is_on(
    client: AsyncClient, portal_hotel: PortalHotel, room_board_with_lost_found: None
) -> None:
    await submit_login(client, portal_hotel.login)

    page = (await client.get(ROOMS)).text

    block = re.search(r'<template data-steps="reported">(.*?)</template>', page, re.S)
    assert block is not None
    assert f'data-lost-found href="{HOME}/lost-found">Найдена вещь</a>' in block.group(1)
    assert page.count("Найдена вещь") == 1


def test_board_has_every_floor_every_status_and_unique_rooms() -> None:
    rooms = demo_room_board.build_rooms(NOW)

    numbers = [room.number for room in rooms]
    assert len(numbers) == len(set(numbers)) == 13 * 12
    assert {room.floor for room in rooms} == set(range(13, 26))
    assert all(room.number.startswith(str(room.floor)) for room in rooms)
    statuses = {room.status for room in rooms}
    assert statuses == {status.key for status in demo_room_board.STATUSES}
    assert sum(room.status == "due_out" for room in rooms) == 9
    # Статусы рассыпаны: «готовые» есть на большинстве этажей, а не одним блоком.
    assert len({room.floor for room in rooms if room.status == "ready"}) >= 8


def test_transitions_follow_the_turnover_path() -> None:
    """Путь номера — тот же, что у живой доски (spec 0046 §3): выезд →
    проверка → уборка → приёмка → готов; у готового и свободного шагов нет."""
    steps = demo_room_board.STEPS
    assert set(steps) == {status.key for status in demo_room_board.STATUSES}
    path: list[demo_room_board.StatusKey] = []
    status: demo_room_board.StatusKey = "occupied"
    while steps[status]:
        status = steps[status][0].to
        path.append(status)
    assert path == ["reported", "inspected", "cleaning", "cleaned", "ready"]
    assert steps["due_out"][0].to == "reported"
    assert steps["ready"] == () and steps["vacant"] == ()
    remark = [step for step in steps["reported"] if step.remark]
    assert len(remark) == 1 and remark[0].note == "set" and remark[0].to == "inspected"
    assert steps["cleaned"][0].note == "clear"


def test_lines_use_hotel_time_and_dates() -> None:
    rooms = {room.status: room for room in reversed(demo_room_board.build_rooms(NOW))}

    assert rooms["due_out"].line == "Выезд сегодня в 12:00"
    assert rooms["vacant"].line == "Гостя нет"
    assert re.fullmatch(r"Гость до \d\d\.11", rooms["occupied"].line)
    assert re.fullmatch(r"выезд \d\d:\d\d", rooms["reported"].line)
    assert re.fullmatch(r"уборка с \d\d:\d\d", rooms["cleaning"].line)
    notes = [room.note for room in demo_room_board.build_rooms(NOW) if room.note]
    assert notes == ["Минибар: 2 колы. Разбит стакан."]


def test_context_counts_match_rooms() -> None:
    context = demo_room_board.build_demo_room_board_context(_staff(), NOW, lost_found=False)

    assert context["total"] == 156
    assert sum(item["count"] for item in context["statuses"]) == 156
    assert [floor["number"] for floor in context["floors"]] == list(range(13, 26))
    assert context["lost_found_href"] is None
