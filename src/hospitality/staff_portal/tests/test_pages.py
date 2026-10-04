"""httpx-смоук страниц кабинета (spec 0033 §10): аутентифицированность каждой,
ключевые элементы, без сессии → редирект на вход отеля (spec 0037 §3); CSRF-щит
форм и заголовки страниц. Вход, общий вход по коду отеля и выход — test_login.py.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import deactivate_user
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.staff_portal import browser
from hospitality.staff_portal.tests.conftest import (
    HOTEL_NAME,
    HOTEL_SLUG,
    PortalHotel,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

HOTEL_LOGIN = f"/staff/{HOTEL_SLUG}/login"


async def test_home_without_session_redirects_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Spec 0037 §3: slug известен из пути — 401 ведёт на вход ЭТОГО отеля."""
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 303
    assert response.headers["location"] == HOTEL_LOGIN


async def test_home_renders_for_manager(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 200
    assert HOTEL_NAME in response.text
    assert "Аружан Менеджер" in response.text
    assert "Менеджер" in response.text
    # Менеджер видит все три раздела (мини-матрица §3.2); очередь и заселение —
    # живые ссылки (PR D, PR E), сотрудники — ещё «скоро» (PR F).
    assert f'href="/staff/{HOTEL_SLUG}/requests"' in response.text
    assert f'href="/staff/{HOTEL_SLUG}/checkin"' in response.text
    assert "Очередь заявок" in response.text
    assert "Заселение" in response.text
    assert "Сотрудники" in response.text


async def test_staff_role_sees_only_queue_section(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF, display_name="Санжар"
    )
    await submit_login(client, staff_login)
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
    await submit_login(client, portal_hotel.login)
    response = await client.get("/staff/hotel-alien")
    assert response.status_code == 403
    assert "Нет доступа" in response.text
    # Сессия жива — с 403-страницы можно выйти (рекомендация ревью PR #153).
    assert "Выйти" in response.text


async def test_pages_send_no_store_and_frame_protection(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Аутентифицированный HTML не кэшируется и не встраивается во фреймы."""
    entry_page = await client.get("/staff/login")
    login_page = await client.get(HOTEL_LOGIN)
    await submit_login(client, portal_hotel.login)
    home_page = await client.get(f"/staff/{HOTEL_SLUG}")
    for name, response in (("entry", entry_page), ("login", login_page), ("home", home_page)):
        assert response.status_code == 200, name
        assert response.headers["cache-control"] == "no-store", name
        assert response.headers["x-frame-options"] == "DENY", name
        # CSP ужесточена до default-src 'self' с появлением своего JS (PR D,
        # рекомендация ревью PR #153): inline-скрипты и чужие источники запрещены.
        assert (
            response.headers["content-security-policy"]
            == "default-src 'self'; frame-ancestors 'none'"
        ), name


async def test_tenant_context_mismatch_fails_closed(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SERVICE_TOKEN чужого тенанта вместе со staff-cookie → 403, а не страница
    под чужим RLS-контекстом (сверка в _page_context, ревью PR #153)."""
    async with platform_session_scope() as session:
        session.add(Tenant(slug="hotel-b", name="Hotel B"))
    monkeypatch.setenv("SERVICE_TOKEN", "mismatch-token")
    monkeypatch.setenv("SERVICE_TOKEN_TENANT_SLUG", "hotel-b")
    get_settings.cache_clear()
    try:
        await submit_login(client, portal_hotel.login)
        response = await client.get(
            f"/staff/{HOTEL_SLUG}", headers={"Authorization": "Bearer mismatch-token"}
        )
        assert response.status_code == 403
        assert "Нет доступа" in response.text
        # Без чужого токена та же cookie работает.
        assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_cross_origin_post_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """CSRF-щит: POST с чужим Origin отклоняется до обработки формы."""
    response = await client.post(
        HOTEL_LOGIN,
        data={"login": portal_hotel.login, "password": "irrelevant"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403

    await submit_login(client, portal_hotel.login)
    logout = await client.post("/staff/logout", headers={"origin": "https://evil.example"})
    assert logout.status_code == 403
    # Сессия жива — отклонённый POST её не тронул.
    assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200


async def test_opaque_origin_post_is_rejected(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """`Origin: null` — отказ, а не «источника нет» (#164).

    Непрозрачный источник подделывается (форма в песочном iframe), а на логине
    cookie-сессии ещё нет, поэтому SameSite не подстрахует. Своя страница такой
    заголовок присылать не должна — за это отвечает тест ниже.

    Ветка `if origin == "null"` в `browser.py` поведение не меняет: пока
    `Host` есть, `null` резало и старое сравнение `netloc` с ним (у `null`
    `netloc` пуст). Тест закрепляет контракт («непрозрачный источник —
    отказ»), а не эту ветку.
    """
    response = await client.post(
        HOTEL_LOGIN,
        data={"login": portal_hotel.login, "password": "irrelevant"},
        headers={"origin": "null"},
    )
    assert response.status_code == 403


async def test_page_referrer_policy_does_not_null_form_origin(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Политика реферера кабинета обязана оставлять формам настоящий `Origin`.

    Регрессия #164: страницы отдавались с `Referrer-Policy: no-referrer`, а по
    Fetch (§ append a request Origin header) запрос в режиме навигации — любая
    отправка HTML-формы — при этой политике обязан прислать `Origin: null`.
    CSRF-щит видел непрозрачный источник и отвечал 403 на собственные логин,
    логаут, заселение и принятие приглашения: кабинет не пускал никого.

    Тест на статус-код это НЕ ловит: httpx шлёт ровно те заголовки, что дал
    тест, и браузерную связку «политика страницы → значение Origin» не
    воспроизводит. Поэтому проверяется сама политика — и запрещающие значения
    названы поимённо, чтобы «ужесточение» обратно упало здесь, а не на
    staging. `same-origin` тоже запрещён: он шлёт своим же запросам ПОЛНЫЙ
    адрес, то есть утёк бы токен приглашения в `Referer` статики.
    """
    assert browser.PAGE_HEADERS["Referrer-Policy"] == "strict-origin"

    # Страницу входа берём ДО логина: с живой сессией она отвечает редиректом,
    # а у редиректа тела нет и `PAGE_HEADERS` на него не вешаются.
    login_page = await client.get(HOTEL_LOGIN)
    invite_page = await client.get("/staff/invite/no-such-token")
    await submit_login(client, portal_hotel.login)
    home_page = await client.get(f"/staff/{HOTEL_SLUG}")
    for name, response in (
        ("login", login_page),
        ("invite", invite_page),
        ("home", home_page),
    ):
        assert response.headers["referrer-policy"] == "strict-origin", name


async def test_same_origin_post_passes(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    response = await client.post(
        HOTEL_LOGIN,
        data={"login": portal_hotel.login, "password": "wrong-password-1"},
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
    """Деактивация гасит сессию — та же cookie получает редирект на вход (§10)."""
    await submit_login(client, portal_hotel.login)
    assert (await client.get(f"/staff/{HOTEL_SLUG}")).status_code == 200
    await deactivate_user(portal_hotel.user_id, actor_user_id=portal_hotel.user_id)
    response = await client.get(f"/staff/{HOTEL_SLUG}")
    assert response.status_code == 303
    assert response.headers["location"] == HOTEL_LOGIN
