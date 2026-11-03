"""Вид гостевых страниц (#746): общие токены, вес чата, подложка, отель в шапке.

Без БД: страницы — статический HTML, собранный при импорте.
"""

from __future__ import annotations

import math

import pytest

from hospitality.channels.common.consent import CONSENT_LANGUAGES, consent_text
from hospitality.channels.web import page, wallpaper
from hospitality.channels.web.documents import page as documents_page
from hospitality.channels.web.menu import page as menu_page
from hospitality.channels.web.table import page as table_page

# Вес страницы чата до #746 со всеми кнопками (room service, бронь, скрепка) — 15 864 байт.
# Условие issue: новый вид не тяжелее вдвое — гость открывает чат на гостиничном Wi-Fi.
_WEIGHT_BEFORE_746 = 15_864

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


def test_wallpaper_tile_is_stable_and_light() -> None:
    """Плитка узора одна и та же от запуска к запуску и весит единицы КБ."""
    assert wallpaper.tile_svg() == wallpaper.tile_svg()
    assert len(wallpaper.tile_svg()) < 5_000
    assert "--wall-tile" in page.render()


@pytest.mark.parametrize(("x", "y", "scale", "degrees"), [(40, 33, 0.76, -20), (261, 276, 0.9, 24)])
def test_wallpaper_matrix_is_the_same_transform(
    x: float, y: float, scale: float, degrees: int
) -> None:
    """Матрица узора = `translate(x y) scale(s) rotate(r 24 24)` с точностью до пикселя."""
    a, b, c, d, e, f = (float(n) for n in wallpaper._matrix(x, y, scale, degrees)[7:-1].split())
    cos, sin = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    for px, py in ((0, 0), (48, 0), (10, 30), (48, 48)):
        rx, ry = 24 + cos * (px - 24) - sin * (py - 24), 24 + sin * (px - 24) + cos * (py - 24)
        assert abs(a * px + c * py + e - (x + scale * rx)) < 1
        assert abs(b * px + d * py + f - (y + scale * ry)) < 1


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
