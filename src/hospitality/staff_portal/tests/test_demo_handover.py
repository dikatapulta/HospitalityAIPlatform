"""Демо «Передача смены» в кабинете (issue #450): выключатель, роли, бланк.

DoD issue: `ENABLE_DEMO_HANDOVER=false` — страницы нет (404) и карточки на
главной нет; `true` — 200 для ресепшена и менеджера, 403 для рядового
сотрудника; проверки бланка считаются сами, одна ✗ — намеренно. Вкладка
«Лист передачи» (#452) живёт на той же странице за тем же выключателем:
выдуманные строки и история на странице, правки подписываются логином
вошедшего.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.staff_portal import demo_handover, demo_handover_board
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
HANDOVER = f"{HOME}/handover"
NBSP = " "


@pytest.fixture
def demo_handover_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Включённый выключатель на один тест — канон `demo_orders_enabled`."""
    monkeypatch.setenv("ENABLE_DEMO_HANDOVER", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _sign_in_as(
    client: AsyncClient, hotel: PortalHotel, role: StaffRole, service_keys: tuple[str, ...] = ()
) -> None:
    login = unique_login()
    await create_staff_user(login, tenant_id=hotel.tenant_id, role=role, service_keys=service_keys)
    await submit_login(client, login)


async def test_switch_off_means_no_page(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Умолчание — выключено: 404 и без сессии (снаружи демо не видно), и с
    сессией; карточки на главной нет."""
    anonymous = await client.get(HANDOVER)
    await submit_login(client, portal_hotel.login)
    signed_in = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert anonymous.status_code == 404
    assert signed_in.status_code == 404
    assert "Передача смены" not in home.text


async def test_switch_on_without_session_leads_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    response = await client.get(HANDOVER)

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/login"


async def test_manager_sees_form_with_both_shifts(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    await submit_login(client, portal_hotel.login)

    response = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert "Подготовить кассу" in page
    assert "Opera — отчёт оплат" in page
    assert "Счётная машинка — пересчёт по купюрам" in page
    assert "Демо · данные условные" in page
    assert "МЕСТО ДЛЯ ЧЕКА · X-ОТЧЁТ ККМ" in page
    assert 'data-handover-sheet="match"' in page
    assert 'data-handover-sheet="mismatch"' in page
    assert "/staff/static/demo_handover.js?v=" in page
    assert "/staff/static/demo_handover.css?v=" in page
    assert f'href="{HANDOVER}"' in home.text


async def test_manual_mode_formulas_are_in_markup(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    """Ручной режим: кнопка и панель на странице, формулы бланка — атрибутами,
    которые считает demo_handover.js (итог «Сходится» = банки + возврат)."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(HANDOVER)).text

    assert "Ручной режим" in page
    assert "data-handover-manual-panel" in page
    assert "data-handover-clear" in page
    assert 'data-in="refund"' in page
    assert 'data-calc="kaspi halyk jusan refund"' in page
    # «Сходится» (#459): наличные — итог пересчёта по купюрам, таблица на обоих бланках.
    assert page.count('data-calc="count_total"') == 2
    assert page.count('data-in="cnt_20000"') == 2
    assert 'data-check="count_total -x_cash"' in page
    assert 'data-calc="20000*cnt_20000"' in page


async def test_receptionist_sees_demo(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    await _sign_in_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    response = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert response.status_code == 200
    assert 'data-handover-tab="list"' in response.text
    assert f'href="{HANDOVER}"' in home.text


async def test_board_tab_shows_invented_rows_and_history(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    """Лист — вкладка той же страницы: выдуманные строки, история «логин ·
    время — было → стало», примечания, шаблоны для новых записей и логин
    вошедшего, которым JS подпишет его правки."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(HANDOVER)).text

    assert 'data-handover-tab="cash"' in page
    assert 'data-handover-tab="list"' in page
    assert 'id="handover-list"' in page
    assert "Хвосты смены" in page
    assert "Изменения за смену" in page
    assert 'data-board-row="r1"' in page
    assert "Мини-бар и прачечная — оплатит при выезде" in page
    assert f"20{NBSP}000{NBSP}₸" in page
    assert f"300{NBSP}$" in page
    assert f"сумма 15{NBSP}000 → 20{NBSP}000{NBSP}₸" in page
    assert "Добавилась прачечная, гость в курсе." in page
    for colleague in ("DEMO1", "DEMO2", "DEMO3"):
        assert f"<b data-entry-login>{colleague}</b>" in page
    assert f'data-me="{portal_hotel.login}"' in page
    assert 'data-board-template="row"' in page
    assert 'data-board-template="note"' in page
    assert "Сдать смену" in page
    # Компактный лист (#457): открытые — таблицей сверху, закрытые — в
    # свёрнутом блоке «Закрыто» под ними; номер может быть списком.
    closed_block = page.index("data-board-closed")
    assert page.index('data-board-row="r1"') < closed_block < page.index('data-board-row="r5"')
    assert "0710/0712" in page
    assert "/staff/static/demo_handover_board.js?v=" in page
    assert "/staff/static/demo_handover_board.css?v=" in page


async def test_staff_role_is_forbidden(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    """Касса — работа ресепшена и менеджера, как заселение: горничной и
    технику страница отвечает «нет доступа», а на главной карточки нет."""
    await _sign_in_as(client, portal_hotel, StaffRole.STAFF)

    response = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert response.status_code == 403
    assert "Передача смены" not in home.text


async def test_demo_assets_are_served(client: AsyncClient) -> None:
    for name in ("demo_handover", "demo_handover_board"):
        script = await client.get(f"/staff/static/{name}.js")
        styles = await client.get(f"/staff/static/{name}.css")

        assert script.status_code == 200
        assert script.headers["content-type"].startswith("text/javascript")
        assert styles.status_code == 200
        assert styles.headers["content-type"].startswith("text/css")


def _staff(login: str | None = None, display_name: str = "") -> StaffContext:
    return cast(
        StaffContext,
        SimpleNamespace(display_name=display_name, tenant_name="", tenant_slug="", login=login),
    )


def test_board_counts_totals_and_feed() -> None:
    """Плитки листа считаются по строкам: открытые суммы — по валютам, без
    суммы — не в сумме; лента — свежие записи сверху; у каждой строки история
    начинается с «внёс строку»."""
    board = demo_handover_board.build_board_context(_staff(login="BORM"))

    assert board["me"] == "BORM"
    assert board["counts"] == {"all": 8, "open": 6, "closed": 2}
    assert board["open_total"] == f"87{NBSP}500{NBSP}₸ · 300{NBSP}$"
    agos = [item["ago"] for item in board["feed"]]
    assert agos == sorted(agos)
    assert all(row["history"][0]["text"] == "внёс строку" for row in board["rows"])
    assert {row["money"] for row in board["rows"] if row["amount"] == ""} == {"—"}


def test_board_signs_edits_with_name_without_login() -> None:
    """Учётка без логина отеля подписывает правки именем — как журнал изменений."""
    board = demo_handover_board.build_board_context(_staff(display_name="Айгерим"))

    assert board["me"] == "Айгерим"


def _sheets() -> dict[str, dict[str, Any]]:
    staff = _staff()
    context = demo_handover.build_demo_handover_context(staff)
    return {sheet["key"]: sheet for sheet in context["sheets"]}


def test_match_sheet_follows_paper_without_difference() -> None:
    """«Сходится» — формат бумажного бланка «без разницы»: банки, возврат за
    смену прибавляется к итогу, наличные одной суммой; проверок нет."""
    match = _sheets()["match"]

    assert [bank["x_tg"] for bank in match["banks"]] == [
        f"486{NBSP}250,00{NBSP}тг",
        f"312{NBSP}740,00{NBSP}тг",
        f"2{NBSP}847{NBSP}315,50{NBSP}тг",
    ]
    assert match["refund_card"] == f"18{NBSP}450,00{NBSP}тг"
    assert match["card_total_tg"] == f"3{NBSP}664{NBSP}755,50{NBSP}тг"
    assert match["cash_total"] == f"1{NBSP}024{NBSP}300,00{NBSP}тг"
    assert match["count_total"] == f"1{NBSP}024{NBSP}300"  # пересчёт = наличные
    assert match["refund_cash"] == f"0,00{NBSP}тг"  # возврат наличными под ними (#469)
    assert match["card_check"] is None
    assert match["cash_check"] is None
    assert match["problems"] == []


def test_mismatch_checks_follow_paper_rule() -> None:
    """«Опера − X-отчёт − возврат = 0» и «пересчёт = X-отчёт»: разница по
    Jusan объяснена возвратом, наличные сходятся."""
    mismatch = _sheets()["mismatch"]

    assert mismatch["card_check"] == demo_handover.Check(True, "✓ сходится с учётом возврата")
    assert mismatch["cash_check"] == demo_handover.Check(True, "✓ пересчёт = X-отчёт")
    assert mismatch["problems"] == []
    assert mismatch["difference"] == mismatch["refund_card"] == f"41{NBSP}415,25{NBSP}тг"
    assert mismatch["note"].startswith(f"Jusan: по Опере больше на 41{NBSP}415,25{NBSP}тг")


def test_cash_count_adds_up() -> None:
    """Сумма пересчёта — номиналы × количество плюс монеты, итог на бланке и
    в строке «Наличка (пересчёт)» — одно число."""
    mismatch = _sheets()["mismatch"]

    assert mismatch["sums"] == [f"620{NBSP}000", f"240{NBSP}000", f"75{NBSP}000"] + [
        f"24{NBSP}000",
        f"7{NBSP}000",
        f"2{NBSP}000",
    ]
    assert mismatch["count_total"] == f"968{NBSP}200"
    assert mismatch["cash_total"] == f"968{NBSP}200,00{NBSP}тг"
    assert mismatch["bills"] == f"93{NBSP}шт."
