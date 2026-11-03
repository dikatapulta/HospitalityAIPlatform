"""Вид гостевого чата по выбору отеля (spec 0066 §2, §4, §5; #761).

Владелец цветов вариантов подложки и заметности обоев: их читают страница гостя
(добавка к стилям) и предпросмотр кабинета (`staff_portal/chat_look.py`). Ключи
выбора — в ядре (`platform/guest_chat_look.py`). Вариант меняет только подложку —
градиент, из которого картинка обоев «Алматы» (#773) берёт цвет; «Узор» — заметность
картинки. Пузыри, кнопки и стекло общие, поэтому контраст текста от выбора не зависит.

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
    """Подложка одной темы: основа и четыре пятна градиента под картинкой обоев."""

    base: str
    spots: tuple[str, str, str, str]


@dataclass(frozen=True)
class Palette:
    """Вариант подложки: надпись в кабинете, день и ночь."""

    title: str
    day: Wall
    night: Wall


# «Небо и золото» — решение основателя 03.11 (цвета — комментарий в #746). «Изумруд» и
# «Горы на закате» — по образцу консультанта «Обои гостевого чата»: пастельная основа,
# золото или сумерки пятнами. Картинка обоев (#773) берёт цвет из градиента
# (`mix-blend-mode: luminosity` в `chat.css`), поэтому вариант перекрашивает и её.
PALETTES: Final[dict[GuestChatPalette, Palette]] = {
    "sky_gold": Palette(
        "Небо и золото",
        Wall("#dbe8f4", ("#b7d6f1", "#f2e4bd", "#c4def4", "#ecdfc2")),
        Wall("#0c1625", ("#15294a", "#262a33", "#1a3454", "#2b2a1f")),
    ),
    "emerald": Palette(
        "Изумруд",
        Wall("#dcefe6", ("#b5e2cd", "#f2e4bd", "#c6e9d9", "#ecdfc2")),
        Wall("#0b1a16", ("#123a2c", "#262a22", "#174332", "#2b2a1f")),
    ),
    "sunset": Palette(
        "Горы на закате",
        Wall("#f5e3dc", ("#f3c7b4", "#e6d4ef", "#f6d9bf", "#d9d2ee")),
        Wall("#1b1218", ("#3d2027", "#2a2138", "#43281f", "#24203a")),
    ),
}

# Заметность рисунка обоев: надпись в кабинете и множитель к умолчанию `chat.css`.
PATTERNS: Final[dict[GuestChatPattern, tuple[str, float]]] = {
    "off": ("Выключен", 0.0),
    "quiet": ("Тише", 0.6),
    "normal": ("Обычный", 1.0),
    "bright": ("Ярче", 1.5),
}
# «Обычный» — 60 % у картинки, решение в #746 (комментарий консультанта 03.11).
IMAGE_OPACITY: Final = 0.6

# Ночная тема страниц гостя — как её пишет `styles.compact_css`; предпросмотр кабинета
# включает и выключает её подменой этой строки (`page.render_preview`).
DARK_MEDIA: Final = "@media (prefers-color-scheme:dark)"

# Знак отеля картинкой (§5): кружок обрезает по центру; прозрачный PNG — на белом.
_LOGO_CSS: Final = (
    ":is(.mono,.ava) img{width:100%;height:100%;border-radius:50%;object-fit:cover;background:#fff}"
)


def wall_vars(wall: Wall) -> dict[str, str]:
    """CSS-переменные градиента подложки `chat.css` для одной темы."""
    names = ("--wall-base", "--wall-1", "--wall-2", "--wall-3", "--wall-4")
    return dict(zip(names, (wall.base, *wall.spots), strict=True))


def image_opacity(look: GuestChatLook) -> str:
    """Непрозрачность картинки обоев для выбора отеля — числом для CSS (`.36`, `.9`)."""
    value = f"{IMAGE_OPACITY * PATTERNS[look.pattern][1]:.2f}".rstrip("0").rstrip(".")
    return value.removeprefix("0") or "0"


def look_css(look: GuestChatLook) -> str:
    """Добавка к стилям страницы гостя; у умолчания — пустая строка (вес #746)."""
    css = ""
    if look.palette != "sky_gold":
        palette = PALETTES[look.palette]
        day, night = (
            ";".join(f"{k}:{v}" for k, v in wall_vars(wall).items())
            for wall in (palette.day, palette.night)
        )
        css += f":root{{{day}}}{DARK_MEDIA}{{:root{{{night}}}}}"
    if look.pattern == "off":
        css += ".wall img{display:none}"
    elif look.pattern != "normal":
        css += f".wall img.on{{opacity:{image_opacity(look)}}}"
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
