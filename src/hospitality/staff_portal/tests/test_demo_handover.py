"""Демо «Передача смены» в кабинете (issue #450): выключатель, роли, бланк.

DoD issue: `ENABLE_DEMO_HANDOVER=false` — страницы нет (404) и карточки на
главной нет; `true` — 200 для ресепшена и менеджера, 403 для рядового
сотрудника; проверки бланка считаются сами, одна ✗ — намеренно.
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
from hospitality.staff_portal import demo_handover
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

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


async def _sign_in_as(client: AsyncClient, hotel: PortalHotel, role: StaffRole) -> None:
    login = unique_login()
    await create_staff_user(login, tenant_id=hotel.tenant_id, role=role)
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


async def test_receptionist_sees_demo(
    client: AsyncClient, portal_hotel: PortalHotel, demo_handover_enabled: None
) -> None:
    await _sign_in_as(client, portal_hotel, StaffRole.RECEPTIONIST)

    response = await client.get(HANDOVER)
    home = await client.get(HOME)

    assert response.status_code == 200
    assert f'href="{HANDOVER}"' in home.text


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
    script = await client.get("/staff/static/demo_handover.js")
    styles = await client.get("/staff/static/demo_handover.css")

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")


def _sheets() -> dict[str, dict[str, Any]]:
    staff = cast(StaffContext, SimpleNamespace(display_name="", tenant_name="", tenant_slug=""))
    context = demo_handover.build_demo_handover_context(staff)
    return {sheet["key"]: sheet for sheet in context["sheets"]}


def test_checks_follow_paper_rule() -> None:
    """«Опера − X-отчёт − возврат = 0» и «пересчёт = X-отчёт»: в «Сходится»
    карта ✓, а пересчёт намеренно на 100 тг больше; в «Не сходится» разница по
    Jusan объяснена возвратом, наличные сходятся."""
    sheets = _sheets()
    match, mismatch = sheets["match"], sheets["mismatch"]

    assert match["card_check"] == demo_handover.Check(True, "✓ сходится с X-отчётом ККМ")
    assert match["cash_check"] == demo_handover.Check(
        False, f"✗ излишек +100,00{NBSP}тг к X-отчёту"
    )
    assert match["problems"] == [f"✗ излишек +100,00{NBSP}тг к X-отчёту"]
    assert mismatch["card_check"] == demo_handover.Check(True, "✓ сходится с учётом возврата")
    assert mismatch["cash_check"] == demo_handover.Check(True, "✓ пересчёт = X-отчёт")
    assert mismatch["problems"] == []
    assert mismatch["difference"] == mismatch["refund_card"] == f"41{NBSP}415,25{NBSP}тг"
    assert mismatch["note"].startswith(f"Jusan: по Опере больше на 41{NBSP}415,25{NBSP}тг")


def test_cash_count_adds_up() -> None:
    """Сумма пересчёта — номиналы × количество плюс монеты, итог на бланке и
    в строке «Наличка (пересчёт)» — одно число."""
    match = _sheets()["match"]

    assert match["sums"] == [f"760{NBSP}000", f"190{NBSP}000", f"55{NBSP}000"] + [
        f"14{NBSP}000",
        f"4{NBSP}000",
        f"1{NBSP}000",
    ]
    assert match["count_total"] == f"1{NBSP}024{NBSP}400"
    assert match["cash_total"] == f"1{NBSP}024{NBSP}400,00{NBSP}тг"
    assert match["bills"] == f"81{NBSP}шт."
