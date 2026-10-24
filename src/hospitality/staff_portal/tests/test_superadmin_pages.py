"""Суперадмин в кабинете (spec 0044 §2–§5, §12): вход, кнопки, ссылка нового пароля.

Страница входа суперадмина, «Сотрудники» под суперадмином и под менеджером,
JSON-действия сброса и стирания, анонимная страница `/staff/reset/{token}`.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

import httpx
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.platform.superadmin_alerts import ROLE_NAMES
from hospitality.platform.totp import code_at, time_step
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.logging import redact_secrets_in_path
from hospitality.staff_portal.team import ROLE_LABELS
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import PASSWORD, RECEPTION, create_staff_user, unique_login
from tests.test_staff_superadmin import create_superadmin

SAME_ORIGIN = {"origin": "https://test"}
SUPERADMIN_PAGE = f"/staff/{HOTEL_SLUG}/superadmin"
TEAM_PAGE = f"/staff/{HOTEL_SLUG}/team"
TEAM_API = f"/staff/{HOTEL_SLUG}/api/team"
NEW_PASSWORD = "new-horse-battery-staple"


async def _superadmin_session(client: AsyncClient, portal_hotel: PortalHotel) -> uuid.UUID:
    staff_login = unique_login()
    user_id, secret = await create_superadmin(portal_hotel.tenant_id, staff_login)
    response = await client.post(
        SUPERADMIN_PAGE,
        data={
            "login": staff_login.lower(),
            "password": PASSWORD,
            "code": code_at(secret, time_step(utc_now())),
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == TEAM_PAGE
    return user_id


async def _json_post(client: AsyncClient, path: str) -> httpx.Response:
    return await client.post(path, json={}, headers=SAME_ORIGIN)


def test_superadmin_alert_role_names_match_cabinet() -> None:
    """Ядро не импортирует подписи кабинета — совпадение держит этот тест."""
    assert ROLE_NAMES == ROLE_LABELS


def test_reset_token_is_masked_in_logs() -> None:
    assert redact_secrets_in_path("/staff/reset/secret-token") == "/staff/reset/***"


async def test_superadmin_page_renders_form(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.get(SUPERADMIN_PAGE)
    assert response.status_code == 200
    for text in ("Вход суперадмина", "Код из приложения", "обычный вход"):
        assert text in response.text


async def test_superadmin_login_errors(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    staff_login = unique_login()
    await create_superadmin(portal_hotel.tenant_id, staff_login)

    wrong = await client.post(
        SUPERADMIN_PAGE, data={"login": staff_login, "password": PASSWORD, "code": "000000"}
    )
    empty = await client.post(SUPERADMIN_PAGE, data={"login": staff_login, "password": PASSWORD})
    regular_door = await submit_login(client, staff_login)

    assert wrong.status_code == 401 and "Неверный логин, пароль или код." in wrong.text
    assert empty.status_code == 422 and "Введите логин, пароль и код." in empty.text
    assert regular_door.status_code == 401
    assert "Неверный логин или пароль." in regular_door.text


async def test_superadmin_login_rejects_cross_origin_form(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.post(
        SUPERADMIN_PAGE,
        data={"login": "ADMN", "password": PASSWORD, "code": "123456"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


async def test_team_page_shows_superadmin_buttons_only_to_superadmin(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await create_staff_user(
        "AIGU", tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF, display_name="Айгуль"
    )
    await _superadmin_session(client, portal_hotel)
    as_superadmin = await client.get(TEAM_PAGE)

    await submit_login(second_client, portal_hotel.login)
    as_manager = await second_client.get(TEAM_PAGE)

    assert "Сбросить пароль" in as_superadmin.text
    assert "Стереть учётку" in as_superadmin.text
    assert "Сбросить пароль" not in as_manager.text
    assert "Стереть учётку" not in as_manager.text
    # Строка суперадмина видна менеджеру — с пометкой и без кнопок.
    assert "суперадмин" in as_manager.text
    assert as_manager.text.count('data-action="deactivate"') == 1  # только Айгуль


async def test_manager_cannot_call_superadmin_actions(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    member_id = await create_staff_user(
        unique_login(), tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF
    )
    await submit_login(client, portal_hotel.login)
    for action in ("password-reset", "erase"):
        response = await _json_post(client, f"{TEAM_API}/members/{member_id}/{action}")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ERR-AUTH-003"


async def test_reset_link_flow_end_to_end(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    member_login = unique_login()
    member_id = await create_staff_user(
        member_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        service_keys=RECEPTION,
        display_name="Сабина",
    )
    await _superadmin_session(client, portal_hotel)

    issued = await _json_post(client, f"{TEAM_API}/members/{member_id}/password-reset")

    assert issued.status_code == 200
    body = issued.json()
    assert body["login"] == member_login and body["expires_in_hours"] == 24
    reset_path = urlsplit(body["reset_url"]).path
    assert body["reset_url"].startswith(get_settings().public_base_url)
    assert reset_path.startswith("/staff/reset/")

    page = await second_client.get(reset_path)
    assert page.status_code == 200
    assert "Сабина, задайте новый пароль" in page.text and member_login in page.text

    saved = await second_client.post(reset_path, data={"password": NEW_PASSWORD})
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/staff/{HOTEL_SLUG}"

    again = await second_client.get(reset_path)
    assert again.status_code == 410 and "Ссылка больше не действует" in again.text
    relogin = await submit_login(second_client, member_login, NEW_PASSWORD)
    assert relogin.status_code == 303


async def test_unknown_reset_link_is_one_screen(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get("/staff/reset/not-a-token")
    assert response.status_code == 410
    assert "Попросите новую у того, кто её прислал." in response.text


async def test_erase_removes_member_from_team_page(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    member_id = await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Старая Учётка",
    )
    await _superadmin_session(client, portal_hotel)
    assert "Старая Учётка" in (await client.get(TEAM_PAGE)).text

    erased = await _json_post(client, f"{TEAM_API}/members/{member_id}/erase")

    assert erased.status_code == 200
    assert "Старая Учётка" not in (await client.get(TEAM_PAGE)).text


async def test_superadmin_row_is_untouchable_from_cabinet(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    admin_id, _ = await create_superadmin(portal_hotel.tenant_id, unique_login())
    await submit_login(client, portal_hotel.login)
    response = await _json_post(client, f"{TEAM_API}/members/{admin_id}/deactivate")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ERR-AUTH-013"
