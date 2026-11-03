"""Вид гостевых страниц (#746): общие токены, вес чата, подложка, отель в шапке.

Без БД: страницы — статический HTML, собранный при импорте.
"""

from __future__ import annotations

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
