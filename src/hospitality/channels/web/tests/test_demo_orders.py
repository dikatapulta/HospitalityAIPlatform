"""Демо «Заказать в номер» (issue #438): выключатель и вход.

DoD issue: `ENABLE_DEMO_ORDERS=false` — страницы нет (404) и кнопки в чате нет;
`true` без гостевой сессии — на страницу чата, где гость входит по коду (новой
двери без входа демо не открывает); `true` с сессией — 200, комната из сессии.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.common.demo_orders import DEMO_ITEMS, DemoSection
from hospitality.channels.web import demo_orders_page
from hospitality.channels.web.demo_orders_extras import ITEM_LOOKS, SECTION_LOOKS
from hospitality.channels.web.demo_orders_texts import DEMO_TEXTS
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, ROOM, WebHotel
from hospitality.shared.config import get_settings

CHAT = f"/g/{HOTEL_SLUG}/{ROOM}"
ORDERS = f"{CHAT}/orders"


@pytest.fixture
def demo_orders_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Включённый выключатель на один тест — канон `service_requests_enabled`
    (`ai/tests/conftest.py`): setenv + сброс `lru_cache` настроек до и после."""
    monkeypatch.setenv("ENABLE_DEMO_ORDERS", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
async def client(web_hotel: WebHotel) -> AsyncIterator[AsyncClient]:
    del web_hotel  # нужен ради тенанта и заселённой комнаты
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client


async def _sign_in(client: AsyncClient, web_hotel: WebHotel) -> None:
    response = await client.post(f"{CHAT}/session", json={"code": web_hotel.access_code})
    assert response.status_code == 200, response.text


async def test_switch_off_means_no_page_and_no_button(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    """Умолчание везде — выключено: страницы нет даже у гостя с сессией, а 404
    тот же, что у неизвестного адреса, — снаружи демо не видно."""
    await _sign_in(client, web_hotel)

    response = await client.get(ORDERS)
    unknown = await client.get(f"{CHAT}/no-such-page")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == unknown.json()["error"]["code"]
    chat = await client.get(CHAT)
    assert 'id="orders-link"' not in chat.text
    assert "__DEMO_ORDERS_LINK__" not in chat.text


async def test_switch_on_without_session_leads_to_sign_in(
    client: AsyncClient, demo_orders_enabled: None
) -> None:
    response = await client.get(ORDERS)

    assert response.status_code == 303
    assert response.headers["location"] == CHAT


async def test_switch_on_unknown_hotel_is_404(
    client: AsyncClient, demo_orders_enabled: None
) -> None:
    response = await client.get(f"/g/no-such-hotel/{ROOM}/orders")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ERR-WEB-001"


async def test_switch_on_with_session_renders_demo(
    client: AsyncClient, web_hotel: WebHotel, demo_orders_enabled: None
) -> None:
    """Страница и кнопка в чате; комната — из сессии, а не из адреса (канон
    канала: гость под чужим QR действует от своего Stay)."""
    await _sign_in(client, web_hotel)

    response = await client.get(f"/g/{HOTEL_SLUG}/999/orders")

    assert response.status_code == 200
    data = json.loads(response.text.split('type="application/json">')[1].split("</script>")[0])
    assert data["room"] == ROOM
    assert [section["key"] for section in data["sections"]] == ["restaurant", "shop"]
    assert set(data["items"]) == set(DEMO_ITEMS)
    assert data["texts"]["demo"] == {"ru": "Демо", "kk": "Демо", "en": "Demo"}
    chat = await client.get(CHAT)
    assert 'id="orders-link"' in chat.text


def test_payload_cannot_close_its_script_tag() -> None:
    """Комнату вносит персонал: `</script>` в номере не должен вырваться из JSON."""
    html = demo_orders_page.render("</script><script>alert(1)</script>")

    payload = html.split('type="application/json">')[1].split("</script>")[0]
    assert json.loads(payload)["room"] == "</script><script>alert(1)</script>"


def test_texts_and_catalog_have_all_three_languages() -> None:
    """Тексты гостю — ru, kk, en, как остальные тексты веб-чата: пустая строка
    на одном из языков показала бы гостю дыру."""
    looks = ITEM_LOOKS.values()
    texts = [*DEMO_TEXTS.values(), *(item.name for item in DEMO_ITEMS.values())]
    texts += [look.desc for look in looks] + [look.meta for look in looks if look.meta]
    for text in texts:
        assert text.ru and text.kk and text.en


def test_every_item_has_a_storefront_look() -> None:
    """Витрина гостя (#440) — на каждую позицию общего каталога, и только на них:
    без описания карточка пуста, а лишний ключ — опечатка. Цвета идут в атрибут
    `style`, поэтому строго `#rrggbb`."""
    assert set(ITEM_LOOKS) == set(DEMO_ITEMS)
    for look in ITEM_LOOKS.values():
        assert all(re.fullmatch(r"#[0-9a-f]{6}", color) for color in look.colors)


def test_section_lists_use_items_of_that_section() -> None:
    """«Популярное» и «Добавить к заказу» не тянут товар из чужой корзины."""
    payload = json.loads(demo_orders_page._payload("1"))
    for section in payload["sections"]:
        own = {key for category in section["categories"] for key in category["items"]}
        look = SECTION_LOOKS[DemoSection(section["key"])]
        assert set(look.popular) <= own
        assert set(look.upsell) <= own


def test_script_uses_only_existing_texts() -> None:
    """Ключ надписи с опечаткой уронил бы страницу на `t()` прямо у гостя."""
    found = re.findall(r'\bt\("([a-z_]+)"', demo_orders_page.JS)
    used = {key for key in found if not key.endswith("_")}  # префиксы — ниже, по разделам
    sections = [section.value for section in DemoSection]
    used |= {
        f"{prefix}_{key}"
        for prefix in ("hero", "eta", "search", "cart", "tab", "text_paid")
        for key in sections
    }
    used |= {f"badge_{look.badge}" for look in ITEM_LOOKS.values() if look.badge}
    used |= {"step_accepted", "step_paid", "step_delivering", "method_cash", "method_card"}
    used |= {"cash", "card", "title", "demo", "demo_note"}
    assert used <= set(DEMO_TEXTS)


def test_assets_cannot_close_their_tags() -> None:
    """Стили и скрипт вклеены в `<style>` и `<script>`: закрывающий тег внутри
    файла оборвал бы страницу."""
    assert "</style" not in demo_orders_page.CSS.lower()
    assert "</script" not in demo_orders_page.JS.lower()
    assert "__DEMO_DATA__" not in demo_orders_page.CSS + demo_orders_page.JS
