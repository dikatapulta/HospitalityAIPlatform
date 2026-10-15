"""Демо «Заказать в номер» (issue #438): выключатель и вход.

DoD issue: `ENABLE_DEMO_ORDERS=false` — страницы нет (404) и кнопки в чате нет;
`true` без гостевой сессии — на страницу чата, где гость входит по коду (новой
двери без входа демо не открывает); `true` с сессией — 200, комната из сессии.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.common.demo_orders import DEMO_ITEMS
from hospitality.channels.web import demo_orders_page
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
    assert {section["section"] for section in data["catalog"]} == {"restaurant", "shop"}
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
    for text in [*DEMO_TEXTS.values(), *(item.name for item in DEMO_ITEMS.values())]:
        assert text.ru and text.kk and text.en
