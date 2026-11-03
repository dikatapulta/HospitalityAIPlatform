"""Вид гостевого чата по выбору отеля на странице гостя (spec 0066 §5, §8; #761).

Без БД: страница, добавка стилей и предпросмотр — чистые функции. Выдача знака по
HTTP и правка из кабинета — `staff_portal/tests/test_chat_look.py`.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from hospitality.channels.web import look, page
from hospitality.platform.guest_chat_look import GuestChatLook

_CHAT_CSS = (Path(look.__file__).resolve().parent / "assets" / "chat.css").read_text("utf-8")
_LOGO = "/g/demo-hotel/logo/0b6f5c3e-8a43-4f0e-9d0e-3f1f5a6b7c8d"
_SAMPLE = ("Номер 1507", "Сегодня", "Можно ещё два полотенца?", "Да, передали.")


def test_default_look_adds_nothing_to_the_page() -> None:
    """Умолчание — страница #746 байт в байт: условие веса проверяется на ней."""
    assert look.look_css(GuestChatLook()) == ""
    assert page.render(look=GuestChatLook()) == page.render()
    assert page.render_bind(look=GuestChatLook()) == page.render_bind()


def test_default_palette_matches_chat_css() -> None:
    """«Небо и золото» и заметность обоев объявлены дважды — в `look.py` и в `chat.css`;
    расходиться им нельзя."""
    palette = look.PALETTES["sky_gold"]
    day, night = look.wall_vars(palette.day), look.wall_vars(palette.night)
    for name in day:
        assert re.findall(rf"{name}: ([^;]+);", _CHAT_CSS) == [day[name], night[name]], name
    assert re.findall(r"\.wall img\.on \{ opacity: ([^;]+); \}", _CHAT_CSS) == [
        look.image_opacity(GuestChatLook())
    ]


@pytest.mark.parametrize(
    ("palette", "pattern", "expected", "absent"),
    [
        ("emerald", "normal", (":root{--wall-base:#dcefe6;", "{:root{--wall-base:#0b1a16;"), "img"),
        ("sunset", "bright", ("--wall-base:#f5e3dc;", ".wall img.on{opacity:.9}"), "display"),
        ("sky_gold", "quiet", (".wall img.on{opacity:.36}",), "--wall-base"),
        ("sky_gold", "off", (".wall img{display:none}",), "--wall-base"),
    ],
)
def test_choice_overrides_wall(
    palette: str, pattern: str, expected: tuple[str, ...], absent: str
) -> None:
    chosen = GuestChatLook.model_validate({"palette": palette, "pattern": pattern})
    css = look.look_css(chosen)

    assert css in page.render(look=chosen)
    for piece in expected:
        assert piece in css
    assert absent not in css


def test_palettes_cover_every_key_with_both_themes() -> None:
    """Каждый ключ схемы — вариант с днём и ночью; неизвестный ключ схема не примет."""
    for key in ("sky_gold", "emerald", "sunset"):
        palette = look.PALETTES[key]
        for wall in (palette.day, palette.night):
            assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in (wall.base, *wall.spots))
    assert set(look.PALETTES) == {"sky_gold", "emerald", "sunset"}
    with pytest.raises(ValidationError):
        GuestChatLook.model_validate({"palette": "pink"})


def test_logo_replaces_letter_in_header_tab_and_bind_page() -> None:
    chosen = GuestChatLook(logo_file_id=uuid.uuid4())
    chat = page.render(hotel_name="Казахстан", look=chosen, logo_url=_LOGO)
    bind = page.render_bind(hotel_name="Казахстан", look=chosen, logo_url=_LOGO)

    for html in (chat, bind):
        assert f'aria-hidden="true"><img src="{_LOGO}" alt=""></span>' in html
        assert f'<link rel="icon" href="{_LOGO}">' in html
        assert ":is(.mono,.ava) img{" in html
    assert look.logo_url("demo-hotel", chosen) == f"/g/demo-hotel/logo/{chosen.logo_file_id}"
    assert look.logo_url("demo-hotel", GuestChatLook()) is None


@pytest.mark.parametrize(("night", "media"), [(True, "@media all"), (False, "@media not all")])
def test_preview_forces_theme_and_carries_no_script(night: bool, media: str) -> None:
    chosen = GuestChatLook(palette="sunset", logo_file_id=uuid.uuid4())
    html = page.render_preview(
        hotel_name="<Отель>", look=chosen, logo_url=_LOGO, night=night, sample=_SAMPLE
    )

    assert look.DARK_MEDIA not in html
    assert media + "{:root{--wall-base:" in html
    assert "<script" not in html and "data-src" not in html
    assert '<img alt="" class="on" src="/g/static/almaty-wallpaper.jpg?v=' in html
    assert "&lt;Отель&gt;" in html and "Можно ещё два полотенца?" in html
    assert html.count(f'<img src="{_LOGO}" alt="">') == 2  # шапка и ответ отеля
