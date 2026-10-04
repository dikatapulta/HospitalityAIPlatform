"""Вход в кабинет по логину отеля (spec 0037 §3, §7, §10): страница входа
отеля, общий вход по коду отеля и cookie `staff_hotel`, вход при живой
сессии (`?switch=1`, отзыв прежней сессии), выбор отеля, выход.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.platform.models import StaffRole, Tenant, TenantMembership, UserIdentity
from hospitality.platform.staff_auth import login, resolve_staff_session
from hospitality.platform.staff_credentials import password_external_id
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.staff_portal.tests.conftest import (
    HOTEL_NAME,
    HOTEL_SLUG,
    PortalHotel,
    submit_login,
)
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

HOTEL_LOGIN = f"/staff/{HOTEL_SLUG}/login"
OTHER_SESSION_NOTICE = "На этом устройстве открыт кабинет под другим"
UNKNOWN_HOTEL_TEXT = "Отель с таким кодом не найден"


async def _second_hotel(slug: str = "hotel-b") -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name="Hotel B")
        session.add(tenant)
        await session.flush()
        return tenant.id


def _fake_redis(monkeypatch: pytest.MonkeyPatch, *, account: int, ip: int) -> FakeRateLimitRedis:
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", str(account))
    monkeypatch.setenv("STAFF_LOGIN_IP_RATE_LIMIT_ATTEMPTS", str(ip))
    fake_redis = FakeRateLimitRedis()  # один на тест: счёт должен накапливаться
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    get_settings.cache_clear()
    return fake_redis


# --------------------------------------------------------------------------
# Страница входа отеля
# --------------------------------------------------------------------------


async def test_hotel_login_page_renders(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.get(HOTEL_LOGIN)

    assert response.status_code == 200
    assert HOTEL_NAME in response.text  # шапка — название отеля
    assert "Вход для персонала" in response.text
    assert 'name="login"' in response.text
    assert 'placeholder="Например, BORM"' in response.text
    assert 'name="password"' in response.text
    assert "Нет доступа? Попросите менеджера отправить вам приглашение." in response.text
    assert 'href="/staff/login?change=1"' in response.text  # «Другой отель»
    assert OTHER_SESSION_NOTICE not in response.text


async def test_unknown_hotel_login_page_is_404_code_form(client: AsyncClient) -> None:
    response = await client.get("/staff/no-such-hotel/login")
    assert response.status_code == 404
    assert UNKNOWN_HOTEL_TEXT in response.text
    assert 'name="hotel"' in response.text


async def test_login_succeeds_and_sets_session_and_hotel_cookies(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await submit_login(client, f" {portal_hotel.login.lower()} ")

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("staff_session="))
    hotel_cookie = next(c for c in cookies if c.startswith("staff_hotel="))
    # Контракт cookie сессии — ревью PR #148 (spec 0033 §3.3).
    for cookie in (session_cookie, hotel_cookie):
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "samesite=lax" in cookie.lower()
        assert "Path=/staff" in cookie
    # Отель браузера — slug и год (spec 0037 §3).
    assert hotel_cookie.startswith(f"staff_hotel={HOTEL_SLUG};")
    assert f"Max-Age={365 * 86400}" in hotel_cookie


async def test_wrong_password_and_unknown_login_are_indistinguishable(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    wrong_password = await submit_login(client, portal_hotel.login, password="wrong-password-1")
    unknown_login = await submit_login(client, unique_login())

    for response in (wrong_password, unknown_login):
        assert response.status_code == 401
        assert "Неверный логин или пароль." in response.text
        assert "staff_session" not in response.cookies
        assert "staff_hotel" not in response.cookies  # cookie отеля — только успехом


async def test_login_of_other_hotel_does_not_open_this_one(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Логин уникален внутри отеля (§2): учётка соседнего отеля на этой
    странице — «нет такого логина»."""
    other_id = await _second_hotel()
    stranger = unique_login()
    await create_staff_user(stranger, tenant_id=other_id, role=StaffRole.MANAGER)

    response = await submit_login(client, stranger)

    assert response.status_code == 401
    assert (await submit_login(client, stranger, tenant_slug="hotel-b")).status_code == 303


async def test_wrong_login_format_is_hint_and_spends_no_budget(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_redis = _fake_redis(monkeypatch, account=1, ip=1)
    try:
        response = await submit_login(client, "ИЩКЬ")
        assert response.status_code == 422
        assert "Проверьте раскладку клавиатуры" in response.text
        assert "ИЩКЬ" in response.text  # введённое сохраняется
        assert fake_redis.counters == {}
    finally:
        get_settings.cache_clear()


async def test_login_without_fields_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.post(HOTEL_LOGIN, data={"login": portal_hotel.login})
    assert response.status_code == 422
    assert "Введите логин и пароль." in response.text


async def test_unknown_hotel_login_post_is_404(client: AsyncClient) -> None:
    response = await client.post(
        "/staff/no-such-hotel/login", data={"login": "BORM", "password": PASSWORD}
    )
    assert response.status_code == 404
    assert UNKNOWN_HOTEL_TEXT in response.text


async def test_login_limit_counts_the_real_client_behind_the_tunnel(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #207: за Cloudflare-туннелем адрес сокета — соседний контейнер,
    и без разбора `CF-Connecting-IP` один сотрудник, забывший пароль, закрывал
    бы вход всему отелю. Проверяем на самой двери, а не на сервисе: заголовок
    читает HTTP-слой кабинета.
    """
    fake_redis = _fake_redis(monkeypatch, account=10, ip=1)  # ключ учётки не мешает
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "127.0.0.1")  # адрес ASGI-транспорта
    get_settings.cache_clear()
    try:

        async def attempt(password: str, ip: str) -> int:
            response = await client.post(
                HOTEL_LOGIN,
                data={"login": portal_hotel.login, "password": password},
                headers={"CF-Connecting-IP": ip},
            )
            return response.status_code

        assert await attempt("wrong-password-1", "203.0.113.10") == 401
        # Другой сотрудник того же отеля — другой адрес, свой бюджет.
        assert await attempt(PASSWORD, "203.0.113.11") == 303
        # А забывчивому его собственный бюджет уже закрыт.
        again = await client.post(
            HOTEL_LOGIN,
            data={"login": portal_hotel.login, "password": PASSWORD},
            headers={"CF-Connecting-IP": "203.0.113.10"},
        )
        assert again.status_code == 429
        assert "Слишком много неудачных попыток входа" in again.text
        assert any("203.0.113.10" in key for key in fake_redis.counters)
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Общий вход /staff/login — код отеля и cookie staff_hotel
# --------------------------------------------------------------------------


def _hotel_cookie(slug: str) -> dict[str, str]:
    return {"cookie": f"staff_hotel={slug}"}


async def test_entry_without_cookie_shows_code_form(client: AsyncClient) -> None:
    response = await client.get("/staff/login")

    assert response.status_code == 200
    assert "Вход для персонала" in response.text
    assert "Код отеля" in response.text
    assert 'placeholder="Например, myhotel"' in response.text
    assert "Код отеля знает менеджер. Браузер запомнит его — вводить нужно один раз." in (
        response.text
    )
    assert "Далее" in response.text


async def test_entry_with_hotel_cookie_goes_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get("/staff/login", headers=_hotel_cookie(HOTEL_SLUG))
    assert response.status_code == 303
    assert response.headers["location"] == HOTEL_LOGIN


@pytest.mark.parametrize("query", ["", "?change=1"])
async def test_entry_ignores_cookie_of_missing_hotel_and_on_change(
    client: AsyncClient, portal_hotel: PortalHotel, query: str
) -> None:
    """Cookie несуществующего отеля — форма кода; `?change=1` — форма кода
    даже при живой cookie («Другой отель» не возвращает на прежний)."""
    slug = HOTEL_SLUG if query else "gone-hotel"
    response = await client.get(f"/staff/login{query}", headers=_hotel_cookie(slug))
    assert response.status_code == 200
    assert 'name="hotel"' in response.text


async def test_entry_hotel_code_is_case_and_space_insensitive(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get("/staff/login", params={"hotel": f"  {HOTEL_SLUG.upper()} "})
    assert response.status_code == 303
    assert response.headers["location"] == HOTEL_LOGIN


async def test_entry_unknown_hotel_code_shows_error(client: AsyncClient) -> None:
    response = await client.get("/staff/login", params={"hotel": "nope"})
    assert response.status_code == 404
    assert UNKNOWN_HOTEL_TEXT in response.text
    assert 'value="nope"' in response.text


async def test_entry_hotel_code_wins_over_cookie(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """`?hotel=` разбирается первым: cookie A + код B → B; cookie A +
    неизвестный код → форма с ошибкой, а не 303 на A."""
    await _second_hotel()
    to_b = await client.get(
        "/staff/login", params={"hotel": "hotel-b"}, headers=_hotel_cookie(HOTEL_SLUG)
    )
    assert to_b.status_code == 303
    assert to_b.headers["location"] == "/staff/hotel-b/login"

    unknown = await client.get(
        "/staff/login", params={"hotel": "typo"}, headers=_hotel_cookie(HOTEL_SLUG)
    )
    assert unknown.status_code == 404
    assert UNKNOWN_HOTEL_TEXT in unknown.text


async def test_hotel_cookie_is_set_only_by_successful_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await client.get(HOTEL_LOGIN)
    await client.get("/staff/login", params={"hotel": HOTEL_SLUG})
    await submit_login(client, portal_hotel.login, password="wrong-password-1")
    assert client.cookies.get("staff_hotel") is None

    await submit_login(client, portal_hotel.login)
    assert client.cookies.get("staff_hotel") == HOTEL_SLUG


# --------------------------------------------------------------------------
# Вход отеля при живой сессии, выбор отеля, выход
# --------------------------------------------------------------------------


async def test_hotel_login_with_member_session_goes_to_cabinet(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)

    response = await client.get(HOTEL_LOGIN)
    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}"

    switching = await client.get(f"{HOTEL_LOGIN}?switch=1")
    assert switching.status_code == 200
    assert OTHER_SESSION_NOTICE in switching.text


async def test_switch_without_session_is_plain_form(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    response = await client.get(f"{HOTEL_LOGIN}?switch=1")
    assert response.status_code == 200
    assert OTHER_SESSION_NOTICE not in response.text


async def test_login_into_other_hotel_replaces_and_revokes_previous_session(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Spec 0037 §3: чужой отель при живой сессии — форма со строкой;
    неверный пароль прежнюю сессию не трогает, успешный вход её отзывает."""
    await _second_hotel()
    other_login = unique_login()
    async with platform_session_scope() as session:
        other_id = await session.scalar(select(Tenant.id).where(Tenant.slug == "hotel-b"))
    assert other_id is not None
    await create_staff_user(other_login, tenant_id=other_id, role=StaffRole.STAFF)
    await submit_login(client, portal_hotel.login)
    previous_token = client.cookies.get("staff_session")
    assert previous_token is not None

    page = await client.get("/staff/hotel-b/login")
    assert page.status_code == 200
    assert OTHER_SESSION_NOTICE in page.text

    failed = await submit_login(
        client, other_login, password="wrong-password-1", tenant_slug="hotel-b"
    )
    assert failed.status_code == 401
    assert OTHER_SESSION_NOTICE in failed.text
    assert await resolve_staff_session(previous_token) is not None

    switched = await submit_login(client, other_login, tenant_slug="hotel-b")
    assert switched.status_code == 303
    assert switched.headers["location"] == "/staff/hotel-b"
    assert await resolve_staff_session(previous_token) is None
    assert client.cookies.get("staff_hotel") == "hotel-b"
    assert (await client.get("/staff/hotel-b")).status_code == 200


async def test_forbidden_page_links_to_switch_login_for_both_cases(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """403 одинаков для «не та роль» и «нет членства»; ссылка «под другим
    логином» в обоих случаях ведёт на форму со строкой, а не 303 в кабинет."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await _second_hotel("hotel-alien")
    await submit_login(client, staff_login)

    for page, slug in (
        (f"/staff/{HOTEL_SLUG}/checkin", HOTEL_SLUG),
        ("/staff/hotel-alien", "hotel-alien"),
    ):
        forbidden = await client.get(page)
        assert forbidden.status_code == 403
        assert 'href="/staff/">В мой кабинет' in forbidden.text
        switch_url = f"/staff/{slug}/login?switch=1"
        assert f'href="{switch_url}"' in forbidden.text
        assert "Войти в этот отель под другим логином" in forbidden.text

        form = await client.get(switch_url)
        assert form.status_code == 200
        assert OTHER_SESSION_NOTICE in form.text


async def test_select_with_one_membership_goes_to_cabinet(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.get("/staff/")
    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}"
    # Ссылка «К выбору отеля» на главной была бы петлёй — её нет.
    assert "К выбору отеля" not in (await client.get(f"/staff/{HOTEL_SLUG}")).text


async def test_select_with_several_memberships_lists_hotels(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Список остаётся только старым учёткам с несколькими членствами (§3);
    второе членство — напрямую в БД: боевого пути к нему больше нет."""
    second_id = await _second_hotel()
    async with platform_session_scope() as session:
        user_id = await session.scalar(
            select(UserIdentity.user_id).where(
                UserIdentity.external_id
                == password_external_id(portal_hotel.tenant_id, portal_hotel.login)
            )
        )
        assert user_id is not None
        session.add(
            TenantMembership(user_id=user_id, tenant_id=second_id, role_key=StaffRole.MANAGER)
        )
    await submit_login(client, portal_hotel.login)

    select_page = await client.get("/staff/")
    assert select_page.status_code == 200
    assert HOTEL_NAME in select_page.text
    assert "Hotel B" in select_page.text
    assert 'href="/staff/">К выбору отеля' in (await client.get(f"/staff/{HOTEL_SLUG}")).text


async def test_staff_without_slash_redirects_relatively(client: AsyncClient) -> None:
    """Общий адрес `/staff` (spec 0037 §5) — свой маршрут с относительным
    303: редирект Starlette за туннелем уводил на `http://`, где Secure-cookie
    кабинета не живут."""
    response = await client.get("/staff")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/"


async def test_legacy_login_post_goes_to_entry(client: AsyncClient) -> None:
    """Форма email + пароль, открытая до деплоя, получает общий вход, а не 405."""
    response = await client.post("/staff/login", data={"email": "x@hotel.kz", "password": "p"})
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"


@pytest.mark.parametrize("path", ["/staff/login?hotel=%00", "/staff/%00/login"])
async def test_nul_in_hotel_code_is_unknown_hotel_not_500(client: AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 404
    assert UNKNOWN_HOTEL_TEXT in response.text


async def test_select_without_session_redirects_to_entry(client: AsyncClient) -> None:
    response = await client.get("/staff/")
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"


async def test_logout_returns_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200

    response = await client.post("/staff/logout")
    assert response.status_code == 303
    assert response.headers["location"] == HOTEL_LOGIN
    assert client.cookies.get("staff_hotel") == HOTEL_SLUG  # выход отель не забывает

    after = await client.get(f"/staff/{HOTEL_SLUG}")
    assert after.status_code == 303
    assert after.headers["location"] == HOTEL_LOGIN


async def test_logout_without_hotel_cookie_goes_to_entry(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    grant = await login(portal_hotel.tenant_id, portal_hotel.login, PASSWORD, client_ip="ip-x")
    response = await client.post(
        "/staff/logout", headers={"cookie": f"staff_session={grant.session_token}"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/login"
    assert await resolve_staff_session(grant.session_token) is None
