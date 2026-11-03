"""Демо «Номера» в кабинете (issue #603): выключатель, меню, доска и шаги.

DoD issue: `ENABLE_DEMO_ROOM_BOARD=false` — страницы нет (404) и пункта меню
нет; `true` — 200 и пункт «Номера» любому вошедшему сотруднику. Данные доски:
номера румпланов отеля (#674), уникальны, все статусы есть. Шаги — по
таблице `demo_room_board.STEPS`, которую шаблон отдаёт кнопками с `data-to`
(JS своих переходов не знает, поэтому проверяется Python-описание и разметка).
"""

from __future__ import annotations

import math
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
from hospitality.staff_portal import demo_lost_found, demo_room_board, demo_room_plan
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
ROOMS = f"{HOME}/rooms"
# Время показа — фиксированное: строки «выезд 10:40» не зависят от часа прогона (#574).
NOW = datetime(2026, 11, 5, 11, 0, tzinfo=UTC)
# Румпланы (#674): жилые этажи 5, 7–9 и 13–25, 351 номер.
FLOORS = [5, 7, 8, 9, *range(13, 26)]
TOTAL = 351


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
        "Демонстрация: номера — как в отеле, статусы и гости выдуманы. "
        "Настоящая доска появится после подключения Opera" in page
    )
    assert page.count('class="room"') == TOTAL
    assert 'data-room="501"' in page and 'data-room="2518"' in page
    # Номера, которых на румплане нет: этаж без румплана и пропуски нумерации.
    for missing in ("601", "1201", "1301", "1510", "2006", "2601"):
        assert f'data-room="{missing}"' not in page, missing
    assert "/staff/static/demo_room_board.js?v=" in page
    assert "/staff/static/demo_room_board.css?v=" in page
    # Без демо «Забытые вещи» кнопки «Найдена вещь» нет.
    assert "Найдена вещь" not in page
    # Пустое замечание — ошибка под полем, а не подсказка браузера (#640).
    assert "data-remark-form novalidate hidden" in page
    assert "data-remark-error hidden>Напишите, что не так</p>" in page


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
    assert len(numbers) == len(set(numbers)) == TOTAL
    assert sorted({room.floor for room in rooms}) == FLOORS
    assert all(room.number.startswith(str(room.floor)) for room in rooms)
    # Обход шагом раскладки проходит каждый номер ровно раз.
    assert math.gcd(demo_room_board._SPREAD_STEP, TOTAL) == 1
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


def test_guests_live_only_in_occupied_and_due_out_rooms() -> None:
    """Число гостей (#661) — у живущих сейчас: занятый номер и выезд сегодня,
    от одного до четырёх; у остальных статусов гостей нет."""
    rooms = demo_room_board.build_rooms(NOW)

    for room in rooms:
        if room.status in demo_room_board.GUEST_STATUSES:
            assert 1 <= room.guests <= 4, room.number
        else:
            assert room.guests == 0, room.number
    assert {"occupied", "due_out"} == demo_room_board.GUEST_STATUSES
    assert len({room.guests for room in rooms if room.status == "occupied"}) >= 3


async def test_guests_are_drawn_on_the_cell_and_panel(
    client: AsyncClient, portal_hotel: PortalHotel, room_board_enabled: None
) -> None:
    """Плитка: значок и число внутри подписи, для диктора — «Гостей в номере:»;
    у свободного номера числа нет. Панель: строка «Гостей в номере:», JS
    берёт число из `data-guests`. В лицах статусов для JS числа нет."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(ROOMS)).text

    rooms = {room.number: room for room in demo_room_board.build_rooms(NOW)}
    cells = dict(
        re.findall(
            r'<button type="button" class="room" data-room="(\d+)"(.*?)</button>', page, re.S
        )
    )
    assert len(cells) == TOTAL
    for number, cell in cells.items():
        guests = rooms[number].guests
        if guests:
            assert f'data-guests="{guests}"' in cell, number
            assert f'<span class="sr-only">Гостей в номере:</span> {guests}</span>' in cell, number
        else:
            assert 'data-guests=""' in cell, number
            assert "room-guests" not in cell, number
    faces = re.findall(r"<template data-face=.*?</template>", page, re.S)
    assert len(faces) == len(demo_room_board.STATUSES)
    assert not any("room-guests" in face for face in faces)
    assert "data-panel-guests hidden>" in page
    assert "Гостей в номере: <b data-panel-guests-count></b>" in page


def test_context_counts_match_rooms() -> None:
    context = demo_room_board.build_demo_room_board_context(_staff(), NOW, lost_found=False)

    assert context["total"] == TOTAL
    assert sum(item["count"] for item in context["statuses"]) == TOTAL
    assert [floor["number"] for floor in context["floors"]] == FLOORS
    assert context["lost_found_href"] is None


def test_every_side_is_twelve_plan_cells() -> None:
    """Ряд румплана — 12 клеток: угловая, десять обычных, угловая. Люкс
    занимает две-три, пустая — одну; номер ряда — своего этажа."""
    known_types = {"STDT", "STDD", "SUPD", "PRMT", "PRMD", "DLXR", "EXEC", "GRND", "FAMR"}
    for floor, sides in demo_room_plan.PLAN.items():
        assert [side.key for side in sides] == ["mountains", "city"], floor
        assert [side.label for side in sides] == ["Вид на горы", "Вид на город"], floor
        for side in sides:
            spans = [cell.span if cell else 1 for cell in side.cells]
            assert sum(spans) == demo_room_plan.SLOTS_PER_SIDE, (floor, side.key)
            for cell in side.cells:
                if cell is not None:
                    assert cell.number.startswith(str(floor)), cell
                    assert cell.room_type in known_types, cell
                    assert 1 <= cell.span <= 3, cell
    # Пример с румплана 20-го этажа: люкс на три клетки, 2006–2007 нет.
    mountains = [cell for cell in demo_room_plan.PLAN[20][0].cells if cell]
    assert [(cell.number, cell.room_type, cell.span) for cell in mountains][4:7] == [
        ("2005", "SUPD", 1),
        ("2008", "GRND", 3),
        ("2009", "DLXR", 2),
    ]


async def test_cells_carry_plan_side_type_and_span(
    client: AsyncClient, portal_hotel: PortalHotel, room_board_enabled: None
) -> None:
    """Плитка несёт тип кодом из Opera; панель читает этаж, сторону и тип из
    `data-*`. Люкс — `data-span`, пустая клетка — немая заглушка."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(ROOMS)).text

    cell = re.search(
        r'<button type="button" class="room" data-room="2008"(.*?)</button>', page, re.S
    )
    assert cell is not None
    assert 'data-side="вид на горы" data-type="GRND" data-span="3"' in cell.group(1)
    assert '<span class="room-type">GRND</span>' in cell.group(1)
    single = re.search(r'data-room="1303"[^>]*>', page)
    assert single is not None and "data-span" not in single.group(0)
    assert 'data-side="вид на город" data-type="STDT">' in page
    gaps = sum(
        1 for sides in demo_room_plan.PLAN.values() for side in sides for c in side.cells if not c
    )
    assert page.count('<span class="room-gap" aria-hidden="true"></span>') == gaps
    assert page.count('<h3 class="rooms-side-title">Вид на горы</h3>') == len(FLOORS)
    assert "<span data-panel-floor></span> этаж · <span data-panel-side></span>" in page


def test_lost_found_rooms_exist_on_the_board() -> None:
    """«Найдена вещь» ведёт в демо «Забытые вещи» с номером: его находки — тоже
    из номеров румплана, иначе показ сошлётся на номер, которого нет."""
    numbers = {room.number for room in demo_room_board.build_rooms(NOW)}
    finds = demo_lost_found.build_demo_lost_found_context(NOW.date(), None)["finds"]
    assert finds and {find["room"] for find in finds} <= numbers
