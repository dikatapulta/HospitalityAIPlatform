"""Тема кабинета (issue #635): шрифт Golos Text своими файлами, палитра светлой
и тёмной темы — одним местом, в `static/theme.css`.

Тёмная тема записана там дважды: «как в системе» (`prefers-color-scheme`) и
выбор «Тёмная» в меню профиля (`data-theme`). Тесты держат два обещания:
блоки совпадают, и ни один другой файл стилей не знает про тему сам — иначе
его цвета не послушались бы переключателя.
"""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

from hospitality.staff_portal.rendering import STATIC_ASSETS

_STATIC = Path(__file__).resolve().parents[1] / "static"
_DECLARATION = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")


def _block(css: str, opener: str) -> dict[str, str]:
    """Объявления переменных первого блока `{…}` после `opener` (без вложенных)."""
    start = css.index("{", css.index(opener)) + 1
    body = css[start : css.index("}", start)]
    return {name: value.strip() for name, value in _DECLARATION.findall(body)}


def test_dark_theme_is_the_same_for_system_and_choice() -> None:
    css = (_STATIC / "theme.css").read_text(encoding="utf-8")
    light = _block(css, ":root {")
    system_dark = _block(css, ':root:not([data-theme="light"])')
    chosen_dark = _block(css, ':root[data-theme="dark"]')
    assert system_dark == chosen_dark
    # У каждого цвета светлой темы есть тёмная пара — и наоборот.
    assert set(system_dark) == set(light)


def test_no_other_stylesheet_follows_the_system_theme_by_itself() -> None:
    for asset in sorted(_STATIC.glob("*.css")):
        if asset.name == "theme.css":
            continue
        css = asset.read_text(encoding="utf-8")
        assert "prefers-color-scheme" not in css, asset.name
        assert "data-theme" not in css, asset.name


async def test_font_is_served_as_binary_woff2(client: AsyncClient) -> None:
    for subset in ("latin", "latin-ext", "cyrillic", "cyrillic-ext"):
        response = await client.get(f"/staff/static/golos-text-{subset}.woff2")
        assert response.status_code == 200
        assert response.headers["content-type"] == "font/woff2"
        assert response.content[:4] == b"wOF2"
    css = (await client.get("/staff/static/theme.css")).text
    assert 'url("/staff/static/golos-text-cyrillic-ext.woff2")' in css
    assert (_STATIC / "golos-text-OFL.txt").exists()  # лицензия OFL — рядом с файлами


async def test_select_arrow_is_a_same_origin_file(client: AsyncClient) -> None:
    """CSP `default-src 'self'` не пускает картинки data: — стрелка списка файлом."""
    response = await client.get("/staff/static/select-chevron.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert 'url("/staff/static/select-chevron.svg")' in str(STATIC_ASSETS["styles.css"].content)
