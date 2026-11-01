"""Демо «Забытые вещи» в кабинете (issue #604): выключатель, роли, данные, `?room=`.

DoD issue: `ENABLE_DEMO_LOST_FOUND=false` — страницы нет (404) и пункта меню нет;
`true` — 200 и пункт меню у любого вошедшего; находки — уникальные id, все
статусы, без имён людей; номер из `?room=` подставляется в форму и
экранируется. Фото на сервер не уходит: у полей формы нет `name`, CSP пускает
только `blob:`-предпросмотр.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.staff_portal import demo_lost_found
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
LOST_FOUND = f"{HOME}/lost-found"
TODAY = date(2026, 11, 5)


@pytest.fixture
def demo_lost_found_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Включённый выключатель на один тест — канон `test_shell.demo_sections_enabled`."""
    monkeypatch.setenv("ENABLE_DEMO_LOST_FOUND", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _form(page: str) -> str:
    match = re.search(r"<form[^>]*data-lf-form.*?</form>", page, re.S)
    assert match is not None, "no found-item form"
    return match.group(0)


async def test_switch_off_means_no_page(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Умолчание — выключено: 404 и без сессии (снаружи демо не видно), и с
    сессией; пункта меню нет."""
    anonymous = await client.get(LOST_FOUND)
    await submit_login(client, portal_hotel.login)
    signed_in = await client.get(LOST_FOUND)
    home = await client.get(HOME)

    assert anonymous.status_code == 404
    assert signed_in.status_code == 404
    assert "Забытые вещи" not in home.text


async def test_switch_on_without_session_leads_to_hotel_login(
    client: AsyncClient, portal_hotel: PortalHotel, demo_lost_found_enabled: None
) -> None:
    response = await client.get(LOST_FOUND)

    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{HOTEL_SLUG}/login"


async def test_any_staff_sees_page_and_menu_item(
    client: AsyncClient, portal_hotel: PortalHotel, demo_lost_found_enabled: None
) -> None:
    """Находку пишет горничная, ищет ресепшен: страница и пункт меню — у
    рядового сотрудника без служб тоже."""
    login = unique_login()
    await create_staff_user(login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    await submit_login(client, login)

    response = await client.get(LOST_FOUND)
    home = await client.get(HOME)

    assert response.status_code == 200
    assert f'href="{LOST_FOUND}"' in home.text
    page = response.text
    assert "Демонстрация: выдуманные находки." in page
    # Карточки сервера — с номером; шаблон новой находки (<template>) — без.
    assert len(re.findall(r"data-lf-card data-status=\"\w+\" data-room=", page)) == 12
    assert "Документы и карты не фотографируем. Отнесите в сейф ресепшена." in page
    assert "Ничего не нашли за эти даты. Спросите хаускипинг." in page
    assert "/staff/static/demo_lost_found.js?v=" in page
    assert "/staff/static/demo_lost_found.css?v=" in page
    # Пункт меню подсвечен на своей странице и помечен «демо».
    assert re.search(rf'href="{LOST_FOUND}"[^>]*aria-current="page"', page)


async def test_photo_stays_in_browser(
    client: AsyncClient, portal_hotel: PortalHotel, demo_lost_found_enabled: None
) -> None:
    """Форма ничего не отправляет: у полей нет `name`, у формы — `action`/`method`;
    картинки — только свои и `blob:`-предпросмотр, скрипты — только свои."""
    await submit_login(client, portal_hotel.login)

    response = await client.get(LOST_FOUND)
    form = _form(response.text)

    assert 'type="file"' in form
    assert " name=" not in form
    assert "action=" not in form
    assert "method=" not in form
    assert "img-src 'self' blob:" in response.headers["content-security-policy"]
    assert "script-src" not in response.headers["content-security-policy"]


async def test_room_from_link_prefills_open_form(
    client: AsyncClient, portal_hotel: PortalHotel, demo_lost_found_enabled: None
) -> None:
    """С доски «Номера» (#603): `?room=1504` — форма открыта, номер подставлен."""
    await submit_login(client, portal_hotel.login)

    closed = _form((await client.get(LOST_FOUND)).text)
    page = (await client.get(f"{LOST_FOUND}?room=1504")).text
    form = _form(page)

    assert " hidden" in closed.split(">", 1)[0]
    assert " hidden" not in form.split(">", 1)[0]
    assert "data-lf-open-form" in page
    assert 'data-lf-room value="1504"' in form


async def test_room_from_link_is_escaped(
    client: AsyncClient, portal_hotel: PortalHotel, demo_lost_found_enabled: None
) -> None:
    await submit_login(client, portal_hotel.login)

    page = (await client.get(LOST_FOUND, params={"room": '"><script>alert(1)</script>'})).text

    assert "<script>alert" not in page
    assert 'data-lf-room value="&#34;&gt;&lt;script&gt;alert(' in page


def test_finds_cover_every_status_without_names() -> None:
    """Уникальные id, все четыре статуса, номера с этажей 13–25, люди — только
    должностями; документ и карта — без картинки и в сейфе."""
    context = demo_lost_found.build_demo_lost_found_context(TODAY, None)
    finds = context["finds"]
    keys = [find["key"] for find in finds]

    assert len(keys) == len(set(keys))
    assert {find["status"] for find in finds} == set(demo_lost_found.STATUS_LABELS)
    for find in finds:
        assert 13 <= int(find["room"]) // 100 <= 25
        for person in (find["finder"], find["returned_by"]):
            assert re.fullmatch(r"[а-яё0-9\- ]*", person), person
    no_photo = [find for find in finds if find["no_photo"]]
    assert {find["title"] for find in no_photo} == {"Документ — паспорт", "Банковская карта"}
    assert {find["storage"] for find in no_photo} == {"сейф ресепшена"}
    assert not context["open_form"]
    assert context["today"] == "2026-11-05"


def test_dates_count_back_from_hotel_today() -> None:
    """Даты — от сегодняшнего дня отеля: «за 7 дней» на показе не пустеет, а
    у выданного гостю — дата выдачи."""
    finds = {
        find["key"]: find
        for find in demo_lost_found.build_demo_lost_found_context(TODAY, None)["finds"]
    }

    assert finds["f01"]["checkout"] == "2026-11-05"
    assert finds["f02"]["checkout_label"] == "4 ноября"
    assert finds["f06"]["status_label"] == "Выдано гостю 04.11"
    assert finds["f12"]["checkout"] == "2026-09-26"
    recent = [f for f in finds.values() if f["checkout"] >= "2026-10-30"]
    assert len(recent) >= 6


def test_room_prefill_is_trimmed() -> None:
    assert demo_lost_found.room_prefill(None) == ""
    assert demo_lost_found.room_prefill("  1504 ") == "1504"
    assert len(demo_lost_found.room_prefill("1" * 100)) == demo_lost_found.ROOM_PREFILL_MAX


async def test_demo_assets_are_served(client: AsyncClient) -> None:
    script = await client.get("/staff/static/demo_lost_found.js")
    styles = await client.get("/staff/static/demo_lost_found.css")

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
