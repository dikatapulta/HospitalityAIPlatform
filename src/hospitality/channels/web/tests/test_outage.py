"""Гость при отказе базы (#991): куда идти — на ресепшен, а не JSON и «Не доставлено».

База «лежит» по-настоящему: приложение ходит в Postgres на закрытый порт, отказ соединения —
тот же, что у остановленного контейнера `db`. Тексты и страница — статический HTML.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.web import outage, page
from hospitality.shared.config import get_settings
from hospitality.shared.db import get_engine
from hospitality.shared.errors import INTERNAL_ERROR_CODE

_BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_TEXTS = re.compile(r'<script id="chat-texts" type="application/json">(.*?)</script>')


def _chat_texts(html: str) -> dict[str, dict[str, str]]:
    found = _TEXTS.search(html)
    assert found is not None
    texts: dict[str, dict[str, str]] = json.loads(found.group(1))
    return texts


def test_chat_page_carries_reception_dial_from_load() -> None:
    """Цифра ресепшена — в странице с загрузки: при отказе читать её неоткуда."""
    call = _chat_texts(page.render(reception_room_dial="0"))["call"]
    assert call["ru"] == (
        "Если срочно — позвоните на ресепшен (0 с телефона в номере) или подойдите к стойке."
    )
    assert "(бөлмедегі телефоннан 0)" in call["kk"] and "(dial 0 from" in call["en"]


def test_chat_page_without_dial_still_sends_to_reception() -> None:
    call = _chat_texts(page.render())["call"]
    assert set(call) == {"kk", "ru", "en"} and not any("{dial}" in text for text in call.values())
    assert call["ru"] == (
        "Если срочно — позвоните на ресепшен с телефона в номере или подойдите к стойке."
    )


def test_chat_script_uses_reception_text_on_5xx_and_no_network() -> None:
    """Под недоставленным и на экране входа — строка «куда идти» при 5xx и обрыве сети."""
    assert "const down = (r) => !r.status || r.status >= 500;" in page._JS
    assert 'fail.dataset.down = "1"' in page._JS and 't("call")' in page._JS
    assert "down(r) ? gateDown()" in page._JS


def test_failed_send_keeps_reception_line_and_retry_key_together() -> None:
    """Ветка «Не доставлено» несёт и строку ресепшена (#991), и прежний ключ повтора (#117).

    Обе правки пришли в одну ветку `chat.js` разными PR; слияние уже сводило их конфликтом,
    и потерю ключа повтора (второй ход гостя на повторе) ни один тест не ловил.
    """
    branch = page._JS.split('mine.classList.add("failed");', 1)[1].split("return;", 1)[0]
    assert "failText(fail); mine.after(fail);" in branch
    assert "retry = {text, cid, mine};" in branch


def test_bind_page_shows_reception_on_5xx() -> None:
    html = page.render_bind(reception_room_dial="9")
    expected = outage.gate_down(
        "Қате, қайталап көріңіз · Ошибка, попробуйте ещё раз · Error. Try again", "9"
    )
    assert json.dumps(expected, ensure_ascii=False, separators=(",", ":")) in html
    assert "__GATE_DOWN__" not in html and "(9 с телефона в номере)" in expected


def test_unavailable_page_is_static_and_trilingual() -> None:
    html = html_lib.unescape(page.UNAVAILABLE_PAGE)
    assert "<script" not in html and "__" not in html
    assert "Чат сейчас не работает, попробуйте позже." in html
    assert "The chat isn't working right now" in html and "Чат қазір жұмыс істемей тұр" in html
    assert "позвоните на ресепшен с телефона в номере или подойдите к стойке" in html


@pytest.fixture
def database_down(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Postgres недоступен: порт 1 на этой машине никто не слушает — соединение отвергнуто."""
    monkeypatch.setenv("POSTGRES_PORT", "1")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()


@pytest.fixture
async def client(database_down: None) -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="https://test"
    ) as http:
        yield http
    await get_engine().dispose()


@pytest.mark.parametrize(
    "path",
    ["/g/demo/101", "/g/demo/101/menu", "/w/demo/b/some-token"],
    ids=["qr", "menu", "leaflet"],
)
async def test_guest_page_on_dead_database_is_the_reception_page(
    client: AsyncClient, path: str
) -> None:
    response = await client.get(path, headers={"Accept": _BROWSER_ACCEPT})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == page.UNAVAILABLE_PAGE


async def test_chat_script_request_on_dead_database_stays_json(client: AsyncClient) -> None:
    """`fetch` страницы получает JSON, как раньше: по статусу скрипт показывает свою строку."""
    response = await client.get("/g/demo/101/messages", headers={"Accept": "*/*"})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == INTERNAL_ERROR_CODE
