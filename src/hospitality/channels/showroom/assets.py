"""Статика 3D-витрины (spec 0069 §2.2): просмотрщик, скрипт и стили страницы, three.js.

Канон `channels/web/wallpaper.py`: файлы пакета читаются в память при импорте и
отдаются маршрутом `GET /showroom/static/{имя}` с кэшем навсегда — версию в адресе
меняет содержимое (`?v=<хэш>`). CSP страницы `default-src 'self'`: ничего извне не
грузится, поэтому и скрипты, и стили — свои файлы, а не встроенные в HTML.

three.js — официальная сборка `build/three.module.min.js` пакета `three@0.170.0`
(npm, MIT — `three-LICENSE.txt`), без правок: sha256 `08fd7545…c39e7` сверяется с
файлом из `npm pack three@0.170.0`. Версия — в имени файла: `viewer.js` импортирует
его по постоянному адресу, и новая версия библиотеки — новое имя.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_DIR: Final = Path(__file__).resolve().parent / "assets"
PREFIX: Final = "/showroom/static"
THREE_FILE: Final = "three-0.170.0.module.min.js"

_JS: Final = "text/javascript; charset=utf-8"
_CSS: Final = "text/css; charset=utf-8"
_TEXT: Final = "text/plain; charset=utf-8"

# Файл → MIME-тип. Только эти имена маршрут и отдаёт.
_FILES: Final[dict[str, str]] = {
    THREE_FILE: _JS,
    "three-LICENSE.txt": _TEXT,
    "viewer.js": _JS,
    "showroom.js": _JS,
    "assistant.js": _JS,
    "showroom.css": _CSS,
    "access.js": _JS,
}


@dataclass(frozen=True)
class Asset:
    content: bytes
    media_type: str
    version: str

    @property
    def etag(self) -> str:
        return f'"{self.version}"'


ASSETS: Final[dict[str, Asset]] = {
    name: Asset(
        content=(raw := (_DIR / name).read_bytes()),
        media_type=media_type,
        version=hashlib.sha256(raw).hexdigest()[:12],
    )
    for name, media_type in _FILES.items()
}


def url(name: str) -> str:
    """Адрес файла статики с версией содержимого."""
    return f"{PREFIX}/{name}?v={ASSETS[name].version}"
