"""Поиск и вкладка «за 7 дней» очереди кабинета (spec 0033 §5, #221).

«Что с моей заявкой?» из комнаты 1207 и «вчера просили — никто не пришёл»
находятся строкой поиска (комната или `#N`) и историей за неделю. Фильтр —
в SQL до потолка ленты: иначе нужная заявка тонула бы за срезом.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.shared.db import utc_now
from hospitality.staff_portal import queue, queue_filters
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_queue import _make_request, _post_action

QUEUE_URL = f"/staff/{HOTEL_SLUG}/requests"


@pytest.mark.parametrize(
    ("raw", "room", "number"),
    [
        ("1207", "1207", None),
        ("  1207а ", "1207а", None),
        ("#12", None, 12),
        ("№ 7", None, 7),
        ("#абв", "#абв", None),  # непонятное ищется комнатой и ничего не находит
        ("#123456", "#123456", None),  # длиннее дневного номера не бывает
    ],
)
def test_parse_search(raw: str, room: str | None, number: int | None) -> None:
    found = queue_filters.parse_search(raw)
    assert (found.room_number, found.daily_number) == (room, number)


def test_parse_search_and_tab_tolerate_garbage() -> None:
    """Ссылку с опечаткой мог прислать коллега: не 422, а «поиска нет» / «открытые»."""
    assert queue_filters.parse_search(None) == queue_filters.QueueSearch()
    assert queue_filters.parse_search("   ") == queue_filters.QueueSearch()
    assert len(queue_filters.parse_search("9" * 500).text) == 20
    assert queue_filters.parse_queue_tab("week") == queue_filters.QUEUE_TAB_WEEK
    assert queue_filters.parse_queue_tab("archive") == queue_filters.QUEUE_TAB_OPEN


async def test_search_by_room_ignores_case_and_spaces(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_request(portal_hotel.tenant_id, "полотенца в 1207а", room_number="1207А ")
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.login)

    page = await client.get(QUEUE_URL, params={"q": " 1207а"})
    assert page.status_code == 200
    assert "полотенца в 1207а" in page.text
    assert "убрать 305" not in page.text
    assert 'value="1207а"' in page.text  # поле держит набранное
    assert "Сбросить" in page.text


async def test_search_by_daily_number(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    first = await _make_request(portal_hotel.tenant_id, "первая")
    await _make_request(portal_hotel.tenant_id, "вторая")
    await submit_login(client, portal_hotel.login)

    for raw in (f"#{first.daily_number}", f"№{first.daily_number}"):
        page = await client.get(QUEUE_URL, params={"q": raw})
        assert "первая" in page.text, raw
        assert "вторая" not in page.text, raw


async def test_search_is_applied_before_the_feed_limit(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поиск — в SQL до среза: старая заявка 1207 находится и за потолком ленты."""
    await _make_request(portal_hotel.tenant_id, "старая из 1207", room_number="1207")
    await _make_request(portal_hotel.tenant_id, "свежая из 305")
    monkeypatch.setattr(queue, "_QUEUE_LIMIT", 1)
    await submit_login(client, portal_hotel.login)

    feed = await client.get(QUEUE_URL)
    assert "старая из 1207" not in feed.text
    assert "Показаны 1 самых новых заявок — более старые найдёт поиск" in feed.text

    found = await client.get(QUEUE_URL, params={"q": "1207"})
    assert "старая из 1207" in found.text


async def test_week_tab_shows_open_and_closed(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    done = await _make_request(portal_hotel.tenant_id, "выполненная")
    await _make_request(portal_hotel.tenant_id, "открытая")
    await submit_login(client, portal_hotel.login)
    assert (await _post_action(client, done.id, "claim")).status_code == 200
    assert (await _post_action(client, done.id, "complete")).status_code == 200

    week = await client.get(QUEUE_URL, params={"tab": "week"})
    assert week.status_code == 200
    assert "выполненная" in week.text
    assert "выполнена" in week.text  # статус на карточке
    assert "открытая" in week.text
    assert "За 7 дней" in week.text


async def test_week_tab_starts_at_hotel_midnight_six_days_back(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«За 7 дней» — сегодня и шесть дней до него: на седьмой день заявка уходит."""
    await store_hotel_config(portal_hotel.tenant_id)
    await _make_request(portal_hotel.tenant_id, "неделю назад")
    await submit_login(client, portal_hotel.login)

    monkeypatch.setattr(queue, "utc_now", lambda: utc_now() + timedelta(days=6))
    sixth_day = await client.get(QUEUE_URL, params={"tab": "week"})
    assert "неделю назад" in sixth_day.text

    monkeypatch.setattr(queue, "utc_now", lambda: utc_now() + timedelta(days=7))
    seventh_day = await client.get(QUEUE_URL, params={"tab": "week"})
    assert "неделю назад" not in seventh_day.text
    assert "За 7 дней заявок не было." in seventh_day.text


async def test_nothing_found_explains_and_offers_the_week(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.login)

    open_tab = await client.get(QUEUE_URL, params={"q": "12"})
    assert "Среди открытых заявок по комнате 12 нет." in open_tab.text
    assert "Номер заявки ищите с решёткой: #12." in open_tab.text
    assert f'href="{QUEUE_URL}?tab=week&amp;q=12"' in open_tab.text

    week_tab = await client.get(QUEUE_URL, params={"tab": "week", "q": "#99"})
    assert "За 7 дней заявки #99 нет." in week_tab.text
    assert "Искать за 7 дней" not in week_tab.text
    assert "с решёткой" not in week_tab.text


async def test_search_survives_tabs_chips_and_polling(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Вкладки, чипсы и поллинг держат поиск; «Сбросить» и скрытые поля — фильтры."""
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.login)

    page = await client.get(QUEUE_URL, params={"tab": "week", "q": "305"})
    assert f'href="{QUEUE_URL}?q=305"' in page.text  # вкладка «Открытые»
    assert f'href="{QUEUE_URL}?tab=week&amp;mine=1&amp;q=305"' in page.text  # чипс «Мои»
    assert f'data-fragment-url="{QUEUE_URL}/fragment?tab=week&amp;q=305"' in page.text
    assert f'href="{QUEUE_URL}?tab=week">Сбросить' in page.text
    assert '<input type="hidden" name="tab" value="week">' in page.text

    fragment = await client.get(f"{QUEUE_URL}/fragment", params={"tab": "week", "q": "404"})
    assert "убрать 305" not in fragment.text
    assert "За 7 дней заявок по комнате 404 нет." in fragment.text
