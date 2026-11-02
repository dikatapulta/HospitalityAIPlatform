"""Каркас страниц отеля (v3 — issue #635, до него #462): боковое меню по роли,
текущий пункт, счётчик открытых заявок, профиль с темой и выходом, нижняя
панель телефона и лист «Ещё». Тема и шрифт — tests/test_theme.py.

Меню только прячет ссылки — право проверяет страница (`_page_context`), поэтому
здесь проверяется видимость, а 403 на чужой раздел — тестами самих страниц.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import shell
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"


@pytest.fixture
def demo_sections_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Демо-выключатель кассы на один тест — канон `demo_handover_enabled`."""
    monkeypatch.setenv("ENABLE_DEMO_HANDOVER", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def turnover_board_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Доска «Выезды и уборка» включена (spec 0046 §5) — тот же канон."""
    monkeypatch.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


MENTOR_URL = "https://hr.necturn.com"
CODEX_URL = "https://standart.necturn.com"


@pytest.fixture
def demo_links_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Адреса пунктов-ссылок к показу (#628) заданы — тот же канон."""
    monkeypatch.setenv("DEMO_MENTOR_URL", MENTOR_URL)
    monkeypatch.setenv("DEMO_CODEX_URL", CODEX_URL)
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _menu(html: str) -> str:
    match = re.search(r'<aside class="app-side".*?</aside>', html, re.S)
    assert match is not None, "no side menu"
    return match.group(0)


def _menu_hrefs(html: str) -> list[str]:
    return re.findall(r'class="app-nav-item" href="([^"]+)"', _menu(html))


async def _login_as(
    client: AsyncClient, hotel: PortalHotel, role: StaffRole, service_keys: tuple[str, ...] = ()
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=hotel.tenant_id,
        role=role,
        service_keys=service_keys,
        display_name="Әсел Нурланова",
    )
    await submit_login(client, staff_login)


async def test_menu_follows_the_role_matrix(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    turnover_board_enabled: None,
) -> None:
    """docs/RBAC.md: сотрудник службы — обзор и очередь; ресепшен — плюс
    заселение и выезды; хаускипинг — плюс выезды; менеджер — всё, а
    «Управление» — только у него."""
    await submit_login(client, portal_hotel.login)
    manager = (await client.get(HOME)).text
    assert _menu_hrefs(manager) == [
        HOME,
        f"{HOME}/requests",
        f"{HOME}/checkin",
        f"{HOME}/turnovers",
        f"{HOME}/summary",
        f"{HOME}/knowledge",
        f"{HOME}/changes",
        f"{HOME}/team",
    ]
    assert "Управление" in _menu(manager)

    await _login_as(second_client, portal_hotel, StaffRole.STAFF, RECEPTION)
    reception = (await second_client.get(HOME)).text
    assert _menu_hrefs(reception) == [
        HOME,
        f"{HOME}/requests",
        f"{HOME}/checkin",
        f"{HOME}/turnovers",
    ]
    assert "Управление" not in _menu(reception)

    second_client.cookies.clear()
    await _login_as(second_client, portal_hotel, StaffRole.STAFF)
    staff = (await second_client.get(f"{HOME}/requests")).text
    assert _menu_hrefs(staff) == [HOME, f"{HOME}/requests"]

    # Хаускипинг — ещё «Выезды и уборка» (spec 0046 §4).
    second_client.cookies.clear()
    await _login_as(second_client, portal_hotel, StaffRole.STAFF, ("housekeeping",))
    housekeeping = (await second_client.get(f"{HOME}/requests")).text
    assert _menu_hrefs(housekeeping) == [HOME, f"{HOME}/requests", f"{HOME}/turnovers"]


async def test_demo_sections_appear_only_with_their_switches(
    client: AsyncClient, portal_hotel: PortalHotel, demo_sections_enabled: None
) -> None:
    await submit_login(client, portal_hotel.login)
    page = (await client.get(HOME)).text
    hrefs = _menu_hrefs(page)
    assert f"{HOME}/handover" in hrefs
    # Демо заказов удалено настоящим room service (spec 0038 PR 5).
    assert f"{HOME}/orders" not in hrefs
    assert _menu(page).count("демо") == 1
    # Главная: кнопка «Передать смену».
    assert f'class="btn btn-primary home-handover" href="{HOME}/handover"' in page
    assert 'data-metric="orders"' not in page


async def test_demo_links_are_absent_without_addresses(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """#628: адрес не задан (копия отеля) — пунктов-ссылок нет нигде."""
    await submit_login(client, portal_hotel.login)
    page = (await client.get(HOME)).text
    assert "Кодекс отеля" not in page
    assert 'target="_blank"' not in page
    assert "↗" not in page


async def test_demo_links_open_other_sites_in_a_new_tab(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    demo_links_enabled: None,
) -> None:
    """#628: оба пункта — всем вошедшим, после «Документов для АВР», в новой
    вкладке без `opener`, с ↗ и «демо»; подсветки текущего у них не бывает.
    На «Обзоре» — те же ссылки в «Быстрых действиях»."""
    await _login_as(second_client, portal_hotel, StaffRole.STAFF)
    staff_menu = _menu((await second_client.get(f"{HOME}/requests")).text)
    assert _menu_hrefs(staff_menu) == [HOME, f"{HOME}/requests", MENTOR_URL, CODEX_URL]
    for url in (MENTOR_URL, CODEX_URL):
        assert f'href="{url}" target="_blank" rel="noopener noreferrer">' in staff_menu
    assert 'Наставник <span aria-hidden="true">↗</span>' in staff_menu
    assert 'Кодекс отеля <span aria-hidden="true">↗</span>' in staff_menu
    assert staff_menu.count("демо") == 2

    await submit_login(client, portal_hotel.login)
    home = (await client.get(HOME)).text
    hrefs = _menu_hrefs(home)
    assert (
        hrefs.index(MENTOR_URL) + 1 == hrefs.index(CODEX_URL) == hrefs.index(f"{HOME}/summary") - 1
    )
    assert _menu(home).count('aria-current="page"') == 1
    quick = home.split('id="home-quick-title"', 1)[1]
    for url in (MENTOR_URL, CODEX_URL):
        assert f'href="{url}" target="_blank" rel="noopener noreferrer">' in quick


async def test_real_mentor_section_replaces_the_demo_link(
    client: AsyncClient, portal_hotel: PortalHotel, demo_links_enabled: None
) -> None:
    """#628: у отеля включён настоящий «Наставник» — два «Наставника» в меню
    не бывает; ссылка на кодекс остаётся."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            portal_hotel.tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                mentor_enabled=True,
            ),
        )
    await submit_login(client, portal_hotel.login)
    hrefs = _menu_hrefs((await client.get(HOME)).text)
    assert f"{HOME}/mentor" in hrefs
    assert MENTOR_URL not in hrefs
    assert hrefs[hrefs.index(f"{HOME}/mentor") + 1] == CODEX_URL


async def test_current_page_is_highlighted(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Подсвечен ровно один пункт — раздел открытой страницы; форма новой
    заявки — часть очереди."""
    await submit_login(client, portal_hotel.login)
    for path, current in (
        ("", HOME),
        ("/requests", f"{HOME}/requests"),
        ("/requests/new", f"{HOME}/requests"),
        ("/knowledge", f"{HOME}/knowledge"),
        ("/team", f"{HOME}/team"),
    ):
        menu = _menu((await client.get(f"{HOME}{path}")).text)
        assert menu.count('aria-current="page"') == 1
        assert f'href="{current}" aria-current="page"' in menu


async def test_profile_menu_holds_theme_and_logout(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """#635: профиль внизу меню — буква, имя, роль; в его меню тема «Системная /
    Тёмная / Светлая» и выход POST-формой (CSRF-контракт). Неработающих кнопок
    «скоро» больше нет."""
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)
    page = (await client.get(HOME)).text
    menu = _menu(page)
    assert '<span class="app-avatar" aria-hidden="true">Ә</span>' in menu
    assert "Әсел Нурланова" in menu
    assert "Сотрудник" in menu
    profile_menu = menu.split('id="app-profile-menu"', 1)[1]
    for choice in ("system", "dark", "light"):
        assert f'data-theme-choice="{choice}"' in profile_menu
    assert '<form method="post" action="/staff/logout">' in profile_menu
    top = page.split('<header class="app-top">', 1)[1].split("</header>", 1)[0]
    for frame in (menu, top):
        assert "скоро" not in frame
        assert " disabled" not in frame
    # Колокольчик — только при пуше на контуре и спрятан: открывает его push.js,
    # когда телефон подписан.
    assert ("data-push-bell" in page) == ('id="push"' in page)
    bell = re.search(r"<button[^>]*data-push-bell[^>]*>", page)
    assert bell is None or bell.group(0).endswith(" hidden>")
    assert "/staff/static/shell.js" in page.split("</head>", 1)[0]  # тема до отрисовки
    assert 'id="signal"' in page
    assert "style=" not in page  # CSP: инлайн-стили не пропускаются


def _bottom_nav(html: str) -> list[str]:
    match = re.search(r'<nav class="app-bnav".*?</nav>', html, re.S)
    assert match is not None, "no bottom nav"
    return re.findall(r"<span>([^<]+)</span>", match.group(0))


async def test_phone_bottom_nav_takes_first_sections_of_the_role(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    turnover_board_enabled: None,
) -> None:
    """#635: на телефоне — до четырёх разделов роли короткими подписями и «Ещё»;
    остальные разделы — в листе «Ещё», «Ещё» подсвечен на их страницах.
    Круглая «+» ведёт на новую заявку, на самой её странице её нет."""
    await submit_login(client, portal_hotel.login)
    team_page = (await client.get(f"{HOME}/team")).text
    assert _bottom_nav(team_page) == ["Обзор", "Заявки", "Заселение", "Уборка", "Ещё"]
    more_button = re.search(r"<button[^>]*data-more-open[^>]*>", team_page)
    assert more_button is not None
    assert 'aria-current="page"' in more_button.group(0)
    sheet = team_page.split('id="app-more"', 1)[1]
    assert f'href="{HOME}/team" aria-current="page"' in sheet
    assert f'href="{HOME}/checkin"' not in sheet  # он уже в нижней панели
    assert '<form method="post" action="/staff/logout">' in sheet
    assert f'class="app-fab" href="{HOME}/requests/new"' in team_page
    assert 'class="app-fab"' not in (await client.get(f"{HOME}/requests/new")).text

    await _login_as(second_client, portal_hotel, StaffRole.STAFF)
    staff_page = (await second_client.get(f"{HOME}/requests")).text
    assert _bottom_nav(staff_page) == ["Обзор", "Заявки", "Ещё"]
    assert f'<a href="{HOME}/requests" aria-current="page">' in staff_page


async def _open_request(tenant_id: uuid.UUID, category_key: str) -> None:
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get(category_key) or await requests_api.create_category(
            requests_api.RequestCategoryCreate(key=category_key, name=category_key)
        )
        await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="Принести полотенца",
                room_number="305",
            )
        )


async def test_open_requests_count_follows_the_queue_scope(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """#635: число у «Очереди заявок» — открытые заявки служб сотрудника, тот же
    срез, что у ленты (spec 0045 §5); ноль — значка нет."""
    await submit_login(client, portal_hotel.login)
    assert 'class="app-count"' not in (await client.get(HOME)).text

    await _open_request(portal_hotel.tenant_id, "housekeeping")
    await _open_request(portal_hotel.tenant_id, "maintenance")
    manager = await client.get(f"{HOME}/team")
    assert '<span class="app-count" aria-label="открытых: 2">2</span>' in _menu(manager.text)

    await _login_as(second_client, portal_hotel, StaffRole.STAFF, ("housekeeping",))
    maid = (await second_client.get(HOME)).text
    assert '<span class="app-count" aria-label="открытых: 1">1</span>' in _menu(maid)


def test_open_count_label_is_capped() -> None:
    assert shell._count_label(0) is None
    assert shell._count_label(shell.OPEN_COUNT_CAP) == str(shell.OPEN_COUNT_CAP)
    assert shell._count_label(shell.OPEN_COUNT_CAP + 1) == f"{shell.OPEN_COUNT_CAP}+"


async def test_entry_pages_have_no_menu(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Вход — без меню: строить его не по чему, сессии нет; вместо простой шапки —
    отель над карточкой входа (#637)."""
    page = (await client.get(f"{HOME}/login")).text
    assert 'class="app-side"' not in page
    assert 'class="topbar"' not in page
    assert 'class="auth-brand"' in page
    assert "/staff/static/theme.css" in page  # шрифт и палитра — и здесь
    assert "/staff/static/shell.js" in page  # выбранная тема — и здесь
