"""httpx-смоук страниц кабинета (spec 0033 §10): аутентифицированность каждой,
ключевые элементы, без сессии → редирект на логин; cookie-контракт ревью
PR #148 (HttpOnly + Secure + SameSite=Lax + Path=/staff) и CSRF-щит форм.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select

from hospitality.platform.models import StaffRole, Tenant, TenantMembership, UserIdentity
from hospitality.platform.staff_auth import deactivate_user, normalize_email
from hospitality.shared.db import platform_session_scope
from hospitality.staff_portal.tests.conftest import (
    HOTEL_NAME,
    HOTEL_SLUG,
    PortalHotel,
    submit_login,
)
from tests.test_staff_auth import create_staff_user


async def test_login_page_renders(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.get("/staff/login")
    assert response.status_code == 200
    assert "Вход для персонала" in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text


async def test_login_rejects_wrong_password(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await submit_login(client, portal_hotel.email, password="wrong-password-1")
    assert response.status_code == 401
    assert "Неверный email или пароль" in response.text
    assert "staff_session" not in response.cookies


async def test_login_without_fields_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.post("/staff/login", data={"email": portal_hotel.email})
    assert response.status_code == 422
    assert "Введите email и пароль" in response.text


async def test_login_single_membership_redirects_to_tenant(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await submit_login(client, portal_hotel.email)
    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}"
    set_cookie = response.headers["set-cookie"]
    assert "staff_session=" in set_cookie
    # Контракт cookie — ревью PR #148 (spec 0033 §3.3).
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie.lower().replace("samesite", "SameSite")
    assert "Path=/staff" in set_cookie


async def test_login_multiple_memberships_goes_to_select(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    async with platform_session_scope() as session:
        second = Tenant(slug="hotel-b", name="Hotel B")
        session.add(second)
        await session.flush()
        second_id = second.id
    second_email = f"two-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(second_email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    # Второе членство тому же сотруднику — напрямую в БД (боевой путь —
    # принятие инвайта существующим email, ADR-008 инвариант а).
    async with platform_session_scope() as session:
        user_id = await session.scalar(
            select(UserIdentity.user_id).where(
                UserIdentity.external_id == normalize_email(second_email)
            )
        )
        assert user_id is not None
        session.add(
            TenantMembership(user_id=user_id, tenant_id=second_id, role_key=StaffRole.MANAGER)
        )

    response = await submit_login(client, second_email)
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/"

    select_page = await client.get("/staff/")
    assert select_page.status_code == 200
    assert HOTEL_NAME in select_page.text
    assert "Hotel B" in select_page.text


async def test_select_without_session_redirects_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get("/staff/")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"


async def test_home_without_session_redirects_to_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"


async def test_home_renders_for_manager(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await submit_login(client, portal_hotel.email)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 200
    assert HOTEL_NAME in response.text
    assert "Аружан Менеджер" in response.text
    assert "Менеджер" in response.text
    # Менеджер видит все три раздела каркаса (мини-матрица §3.2).
    assert "Очередь заявок" in response.text
    assert "Заселение" in response.text
    assert "Сотрудники" in response.text


async def test_staff_role_sees_only_queue_section(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    email = f"staff-{uuid.uuid4().hex[:8]}@hotel.kz"
    await create_staff_user(
        email, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF, display_name="Санжар"
    )
    await submit_login(client, email)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 200
    assert "Очередь заявок" in response.text
    assert "Заселение" not in response.text
    assert "Сотрудники" not in response.text


async def test_home_foreign_tenant_forbidden(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    async with platform_session_scope() as session:
        session.add(Tenant(slug="hotel-alien", name="Alien Hotel"))
    await submit_login(client, portal_hotel.email)
    response = await client.get("/staff/hotel-alien")
    assert response.status_code == 403
    assert "Нет доступа" in response.text


async def test_login_page_with_session_redirects_to_select(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.email)
    response = await client.get("/staff/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/"


async def test_logout_revokes_session_and_cookie(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.email)
    assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200

    response = await client.post("/staff/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"

    after = await client.get(f"/staff/{HOTEL_SLUG}")
    assert after.status_code == 303
    assert after.headers["location"] == "/staff/login"


async def test_cross_origin_post_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """CSRF-щит: POST с чужим Origin отклоняется до обработки формы."""
    response = await client.post(
        "/staff/login",
        data={"email": portal_hotel.email, "password": "irrelevant"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403

    await submit_login(client, portal_hotel.email)
    logout = await client.post("/staff/logout", headers={"origin": "https://evil.example"})
    assert logout.status_code == 403
    # Сессия жива — отклонённый POST её не тронул.
    assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200


async def test_same_origin_post_passes(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.post(
        "/staff/login",
        data={"email": portal_hotel.email, "password": "wrong-password-1"},
        headers={"origin": "https://test"},
    )
    # Origin совпал с Host — дошли до проверки пароля (401), а не CSRF-403.
    assert response.status_code == 401


async def test_styles_are_served(client: AsyncClient) -> None:
    response = await client.get("/staff/static/styles.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert ":root" in response.text


async def test_deactivated_user_loses_access(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Деактивация гасит сессию — та же cookie получает редирект на логин (§10)."""
    await submit_login(client, portal_hotel.email)
    assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200
    await deactivate_user(portal_hotel.user_id, actor_user_id=portal_hotel.user_id)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"
