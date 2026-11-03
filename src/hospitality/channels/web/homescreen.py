"""Гостевой чат как приложение на экране «Домой» (spec 0068 §2.1, #783).

Манифест — свой у каждой комнаты: иконка открывает ту комнату, с которой её добавили, и поле
комнаты на экране входа уже заполнено. Приложение одно на отель (`id` и `scope` — `/g/{slug}/`),
меню, столик и реквизиты лежат внутри него. Канон — манифест кабинета (spec 0050 §3), но тенантный:
под иконкой — название отеля.

Иконка пока общая для всех отелей (облачко чата на палитре «Небо и золото»); знак отеля вместо
неё — после настройки вида чата (#761). Файлы отдаются по адресу с версией и кэшируются навсегда,
как обои (`wallpaper.py`).
"""

from __future__ import annotations

import hashlib
import html
from pathlib import Path
from typing import Final
from urllib.parse import quote

_ASSETS: Final = Path(__file__).resolve().parent / "assets"

# Маршрут иконок — `router.py`; стоит раньше `/{tenant_slug}/{room_number}`, иначе его съест чат.
ICON_PATH_PREFIX: Final = "/g/static/"
ICONS: Final[dict[str, bytes]] = {
    name: (_ASSETS / name).read_bytes()
    for name in ("chat-icon-180.png", "chat-icon-192.png", "chat-icon-512.png")
}
_VERSIONS: Final = {name: hashlib.sha256(raw).hexdigest()[:12] for name, raw in ICONS.items()}

MEDIA_TYPE: Final = "application/manifest+json"
# Подложка чата (`--wall-base`) — фон заставки при запуске; `--bg` — строка состояния.
_BACKGROUND: Final = "#dbe8f4"
_THEME: Final = "#f2f6fa"


def icon_url(name: str) -> str:
    """Адрес иконки с версией содержимого: новая картинка — новый адрес."""
    return f"{ICON_PATH_PREFIX}{name}?v={_VERSIONS[name]}"


def icon_etag(name: str) -> str:
    return f'"{_VERSIONS[name]}"'


def _room_url(tenant_slug: str, room_number: str) -> str:
    """Страница комнаты: slug и комната из пути уже раскодированы — кодируем заново."""
    return f"/g/{quote(tenant_slug, safe='')}/{quote(room_number, safe='')}"


def manifest(*, hotel_name: str, tenant_slug: str, room_number: str) -> dict[str, object]:
    """Манифест приложения комнаты: под иконкой — название отеля, как в шапке чата."""
    name = hotel_name.strip() or "Hotel"
    scope = f"/g/{quote(tenant_slug, safe='')}/"
    return {
        "id": scope,
        "name": name,
        "short_name": name,
        "start_url": _room_url(tenant_slug, room_number),
        "scope": scope,
        "display": "standalone",
        "background_color": _BACKGROUND,
        "theme_color": _THEME,
        "icons": [
            {"src": icon_url("chat-icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": icon_url("chat-icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {
                "src": icon_url("chat-icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }


def head_links(*, hotel_name: str, tenant_slug: str, room_number: str) -> str:
    """Голова страницы чата: манифест своей комнаты, иконка и подпись для iPhone."""
    href = html.escape(_room_url(tenant_slug, room_number) + "/manifest.webmanifest")
    title = html.escape(hotel_name.strip() or "Hotel")
    return (
        f'<link rel="manifest" href="{href}">\n'
        f'<link rel="apple-touch-icon" href="{icon_url("chat-icon-180.png")}">\n'
        f'<meta name="apple-mobile-web-app-title" content="{title}">'
    )
