"""Вид гостевого чата по выбору отеля (spec 0066 §2, §4, §5; #761).

Владелец цветов вариантов подложки и множителей узора: их читают страница гостя
(добавка к стилям) и предпросмотр кабинета (`staff_portal/chat_look.py`). Ключи
выбора — в ядре (`platform/guest_chat_look.py`). Вариант меняет только подложку —
градиент и цвет узора; пузыри, кнопки и стекло общие, поэтому контраст текста от
выбора не зависит.

Умолчание (`sky_gold` + `normal`, без знака) объявлено и в `assets/chat.css` — страница
с ним не тяжелее ни на байт (условие веса #746); тест держит оба места равными.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.platform.files import ERR_FILE_NOT_FOUND, StoredFile, read_file
from hospitality.platform.guest_chat_look import GuestChatLook, GuestChatPalette, GuestChatPattern
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import current_tenant_id


@dataclass(frozen=True)
class Wall:
    """Подложка одной темы: основа, четыре пятна градиента, цвет линий узора."""

    base: str
    spots: tuple[str, str, str, str]
    ink: str


@dataclass(frozen=True)
class Palette:
    """Вариант подложки: надпись в кабинете, день и ночь."""

    title: str
    day: Wall
    night: Wall


# «Небо и золото» — решение основателя 03.11 (цвета — комментарий в #746). «Изумруд» и
# «Горы на закате» — по образцу консультанта «Обои гостевого чата»: пастельная основа,
# золото или сумерки пятнами, линии узора — тот же тон темнее (днём) или светлее (ночью).
PALETTES: Final[dict[GuestChatPalette, Palette]] = {
    "sky_gold": Palette(
        "Небо и золото",
        Wall("#dbe8f4", ("#b7d6f1", "#f2e4bd", "#c4def4", "#ecdfc2"), "#23527a"),
        Wall("#0c1625", ("#15294a", "#262a33", "#1a3454", "#2b2a1f"), "#b7d2ee"),
    ),
    "emerald": Palette(
        "Изумруд",
        Wall("#dcefe6", ("#b5e2cd", "#f2e4bd", "#c6e9d9", "#ecdfc2"), "#1d5e45"),
        Wall("#0b1a16", ("#123a2c", "#262a22", "#174332", "#2b2a1f"), "#b3e6cf"),
    ),
    "sunset": Palette(
        "Горы на закате",
        Wall("#f5e3dc", ("#f3c7b4", "#e6d4ef", "#f6d9bf", "#d9d2ee"), "#7d3a2c"),
        Wall("#1b1218", ("#3d2027", "#2a2138", "#43281f", "#24203a"), "#f2c9ba"),
    ),
}

# Заметность узора: надпись в кабинете и множитель к умолчанию `chat.css` (#746).
PATTERNS: Final[dict[GuestChatPattern, tuple[str, float]]] = {
    "off": ("Выключен", 0.0),
    "quiet": ("Тише", 0.6),
    "normal": ("Обычный", 1.0),
    "bright": ("Ярче", 1.5),
}
# Узор «Обычный» — выбор консультанта по мандату основателя (#746): 12 % днём, ×0,7 ночью.
INK_OPACITY_DAY: Final = 0.12
INK_OPACITY_NIGHT: Final = 0.084

# Ночная тема страниц гостя — как её пишет `styles.compact_css`; предпросмотр кабинета
# включает и выключает её подменой этой строки (`page.render_preview`).
DARK_MEDIA: Final = "@media (prefers-color-scheme:dark)"

# Знак отеля картинкой (§5): кружок обрезает по центру; прозрачный PNG — на белом.
_LOGO_CSS: Final = (
    ":is(.mono,.ava) img{width:100%;height:100%;border-radius:50%;object-fit:cover;background:#fff}"
)


def wall_vars(wall: Wall, ink_opacity: float) -> dict[str, str]:
    """CSS-переменные подложки `chat.css` для одной темы."""
    names = ("--wall-1", "--wall-2", "--wall-3", "--wall-4")
    return {
        "--wall-base": wall.base,
        **dict(zip(names, wall.spots, strict=True)),
        "--wall-ink": wall.ink,
        "--wall-ink-opacity": f"{ink_opacity:.3f}".rstrip("0").rstrip(".") or "0",
    }


def theme_vars(look: GuestChatLook) -> tuple[dict[str, str], dict[str, str]]:
    """Переменные подложки дня и ночи для выбора отеля."""
    palette = PALETTES[look.palette]
    factor = PATTERNS[look.pattern][1]
    return (
        wall_vars(palette.day, INK_OPACITY_DAY * factor),
        wall_vars(palette.night, INK_OPACITY_NIGHT * factor),
    )


def look_css(look: GuestChatLook) -> str:
    """Добавка к стилям страницы гостя; у умолчания — пустая строка (вес #746)."""
    css = ""
    if look.palette != "sky_gold" or look.pattern != "normal":
        day, night = (";".join(f"{k}:{v}" for k, v in t.items()) for t in theme_vars(look))
        css += f":root{{{day}}}{DARK_MEDIA}{{:root{{{night}}}}}"
    if look.logo_file_id is not None:
        css += _LOGO_CSS
    return css


def logo_url(tenant_slug: str, look: GuestChatLook) -> str | None:
    """Адрес знака для `<img>` и favicon; знака нет — None. Адрес — id файла (§4)."""
    if look.logo_file_id is None:
        return None
    return f"/g/{quote(tenant_slug, safe='')}/logo/{look.logo_file_id}"


async def hotel_look() -> GuestChatLook:
    """Выбор отеля; зовётся внутри `tenant_context`. Конфига нет — умолчание (§5)."""
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return GuestChatLook()
    return config.guest_chat_look


async def read_logo(file_id: uuid.UUID) -> StoredFile:
    """Знак отеля для гостя; зовётся внутри `tenant_context`.

    Отдаётся только файл, что сейчас стоит знаком (§4): иначе этот адрес без
    сессии выдал бы любой файл тенанта по id — фото блюда или документ гостя.
    Другой id, прежний знак, отель без конфига — 404 `ERR-FILES-004`.
    """
    if (await hotel_look()).logo_file_id != file_id:
        raise AppError(code=ERR_FILE_NOT_FOUND, message="File not found", status_code=404)
    return await read_file(file_id)
