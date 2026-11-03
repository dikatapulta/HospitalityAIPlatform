"""Вид гостевых страниц (#746): общие токены, вес чата, обои (#773), отель в шапке.

Без БД: страницы — статический HTML, собранный при импорте.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.common.consent import CONSENT_LANGUAGES, consent_text
from hospitality.channels.web import page, wallpaper
from hospitality.channels.web.documents import page as documents_page
from hospitality.channels.web.menu import page as menu_page
from hospitality.channels.web.table import page as table_page

# Вес страницы чата до #746: со всеми кнопками (room service, бронь, скрепка) — 15 864 байт,
# без них — 14 623. Условие issue: новый вид не тяжелее вдвое — гость открывает чат на
# гостиничном Wi-Fi.
_WEIGHT_BEFORE_746 = 15_864
_WEIGHT_BEFORE_746_PLAIN = 14_623

_FULL_CHAT = page.render(
    hotel_name="Demo Hotel", room_service=True, documents=True, table_booking=True
)
_PAGES = {
    "чат": _FULL_CHAT,
    "листок": page.render_bind(hotel_name="Demo Hotel"),
    "меню": menu_page.render(),
    "столик": table_page.render(),
    "реквизиты": documents_page.render(),
}


@pytest.mark.parametrize("name", _PAGES)
def test_guest_pages_share_one_palette(name: str) -> None:
    """Один набор токенов на все гостевые страницы: акцент «Небо и золото», старой зелени нет."""
    html = _PAGES[name]
    assert "#1f6fb2" in html and "#6fb2ea" in html  # акцент дня и ночи
    assert "#0d7a54" not in html and "#1a73e8" not in html  # зелёный меню и синий старого чата
    assert 'name="theme-color" content="#f2f6fa"' in html


def test_chat_page_weight_at_most_double() -> None:
    assert len(_FULL_CHAT.encode()) <= 2 * _WEIGHT_BEFORE_746
    assert len(page.render().encode()) <= 2 * _WEIGHT_BEFORE_746_PLAIN


def test_chat_styles_carry_only_used_tokens() -> None:
    """Токены меню и реквизитов (шафран, предупреждения) в стили чата не попадают."""
    assert "--saffron" not in page.render() and "--accent:" in page.render()


@pytest.mark.parametrize("name", ["чат", "листок"])
def test_wallpaper_is_a_separate_request(name: str) -> None:
    """Обои (#773) — адресом с версией, а не внутри страницы: вес HTML они не трогают,
    а `src` ставит скрипт после первой отрисовки."""
    html = _PAGES[name]
    assert f'<img alt="" data-src="{wallpaper.URL}">' in html
    assert "data:image/jpeg" not in html and "--wall-tile" not in html


async def test_wallpaper_is_served_cached_forever() -> None:
    """Картинка отдаётся своим маршрутом, а не страницей чата отеля со slug «static»."""
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        response = await client.get(wallpaper.URL)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.content == wallpaper.IMAGE and wallpaper.IMAGE[:3] == b"\xff\xd8\xff"


def test_header_language_left_hotel_mark_right() -> None:
    """Раскладка B+C (#746): пилюля языка слева, название посередине, знак отеля справа."""
    html = _FULL_CHAT
    assert html.index('id="lang"') < html.index('class="title') < html.index('id="mono"')


@pytest.mark.parametrize("room_service", [True, False])
def test_empty_chat_tiles_follow_room_service(room_service: bool) -> None:
    """Пустой чат: две плитки с текстом, третья — меню room service, только если он включён."""
    html = page.render(room_service=room_service)
    assert html.count('data-fill="') == 2
    assert ('class="tile wide" data-path="menu"' in html) is room_service


def test_hotel_name_is_escaped_in_header_and_tab() -> None:
    html = page.render(hotel_name="<b>Отель</b>")
    assert "<title>&lt;b&gt;Отель&lt;/b&gt;</title>" in html
    assert "<b>Отель</b>" not in html


def test_consent_languages_are_separate_paragraphs() -> None:
    """Три версии канона абзацами — без строки «———» между ними (находка ревью #745)."""
    html = page.render()
    assert "———" not in html
    for code in CONSENT_LANGUAGES:
        assert consent_text(code).split(":")[0] in html
