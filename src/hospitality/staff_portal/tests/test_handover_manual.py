"""Касса передачи смены — только ручной бланк (issue #469, гостевая копия).

`ENABLE_HANDOVER_MANUAL` без демо открывает «Передачу смены» с двумя чистыми
бланками: без «Подготовить кассу», листа передачи, выдуманных сумм и пометки
«демо». Возвраты стоят нулём, у «Сходится» есть возврат наличными. Включённое
демо главнее — на staging страница остаётся демо целиком.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.staff_portal import demo_handover
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
HANDOVER = f"{HOME}/handover"
NBSP = "\u00a0"
ZERO = f"0,00{NBSP}тг"


def _switches(monkeypatch: pytest.MonkeyPatch, *, demo: bool) -> Iterator[None]:
    monkeypatch.setenv("ENABLE_HANDOVER_MANUAL", "true")
    monkeypatch.setenv("ENABLE_DEMO_HANDOVER", str(demo).lower())
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def manual_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Как на гостевой копии: ручной бланк включён, демо выключено."""
    yield from _switches(monkeypatch, demo=False)


@pytest.fixture
def manual_and_demo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Как на staging, если там включат оба выключателя."""
    yield from _switches(monkeypatch, demo=True)


async def _sign_in_as(client: AsyncClient, hotel: PortalHotel, role: StaffRole) -> None:
    login = unique_login()
    await create_staff_user(login, tenant_id=hotel.tenant_id, role=role)
    await submit_login(client, login)


def _menu_item(html: str) -> str:
    match = re.search(r'<a class="app-nav-item" href="[^"]*/handover".*?</a>', html, re.S)
    assert match is not None, "no handover item in the menu"
    return match.group(0)


async def test_manual_only_page_is_blank_sheets_without_demo(
    client: AsyncClient, portal_hotel: PortalHotel, manual_only: None
) -> None:
    await submit_login(client, portal_hotel.login)

    response = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert "<title>Передача смены — " in page
    assert "Касса к передаче смены" in page
    assert "data-handover-manual-only" in page
    assert 'class="handover is-manual"' in page
    assert 'data-handover-sheet="match"' in page
    assert 'data-handover-sheet="mismatch"' in page
    assert "data-handover-print" in page
    assert "data-handover-clear" in page
    for demo_part in (
        "Подготовить кассу",
        "Ручной режим",
        "Opera — отчёт оплат",
        "data-handover-tab",
        "Хвосты смены",
        "Демо · данные условные",
        "demo_handover_board",
        f"486{NBSP}250",  # сумма примера «Сходится»
    ):
        assert demo_part not in page, demo_part
    assert "app-tag-demo" not in _menu_item(page)
    assert f'href="{HANDOVER}"' in home.text


async def test_cash_refund_on_match_and_refunds_stand_at_zero(
    client: AsyncClient, portal_hotel: PortalHotel, manual_only: None
) -> None:
    """«Сходится»: под «Наличные» — «Возврат наличными»; все четыре возврата
    (по карте и наличными на обоих бланках) — нулём, а не пустым местом."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(HANDOVER)).text

    assert "Возврат наличными" in page
    assert page.count('data-in="refund_cash"') == 2
    assert page.count("data-zero") == 4
    for name in ("refund", "refund_card", "refund_cash"):
        for field in re.findall(rf'<[^>]*data-in="{name}"[^>]*>([^<]*)<', page):
            assert field == ZERO, name


async def test_receptionist_is_allowed(
    client: AsyncClient, portal_hotel: PortalHotel, manual_only: None
) -> None:
    await _sign_in_as(client, portal_hotel, StaffRole.RECEPTIONIST)

    response = await client.get(HANDOVER)

    assert response.status_code == 200


async def test_staff_role_is_forbidden(
    client: AsyncClient, portal_hotel: PortalHotel, manual_only: None
) -> None:
    await _sign_in_as(client, portal_hotel, StaffRole.STAFF)

    response = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert response.status_code == 403
    assert "Передача смены" not in home.text


async def test_demo_wins_when_both_switches_are_on(
    client: AsyncClient, portal_hotel: PortalHotel, manual_and_demo: None
) -> None:
    await submit_login(client, portal_hotel.login)

    page = (await client.get(HANDOVER)).text

    assert "Подготовить кассу" in page
    assert "data-handover-manual-only" not in page
    assert "app-tag-demo" in _menu_item(page)


def test_blank_sheets_carry_no_amounts() -> None:
    staff = cast(StaffContext, SimpleNamespace(display_name="", tenant_name="", tenant_slug=""))

    context = demo_handover.build_manual_handover_context(staff)

    assert context["manual_only"] is True
    assert "board" not in context
    for sheet in context["sheets"]:
        assert [bank["name"] for bank in sheet["banks"]] == list(demo_handover.BANKS)
        assert {bank["x_tg"] for bank in sheet["banks"]} == {""}
        assert sheet["card_total_tg"] == sheet["cash_total"] == sheet["count_total"] == ""
        assert set(sheet["counts"]) == set(sheet["sums"]) == {""}
        assert sheet["refund_card"] == sheet["refund_cash"] == ZERO
        assert sheet["problems"] == []
