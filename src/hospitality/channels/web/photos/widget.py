"""Скрипт фото к заявке (spec 0081 §4): `assets/photo.js` с его надписями и стилями.

Не вклеивается в страницу чата — канон виджета оценки (`feedback/widget.py`): вес HTML чата
ограничен (#746), а фото нужны только там, где они включены, и только гостю, который снимает
или у которого фото в ленте. Скрипт чата грузит файл по первой надобности; кэш навсегда —
версию в адресе меняет содержимое. Собирается один раз при импорте.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from hospitality.channels.web.styles import compact_css, compact_js

_ASSETS: Final = Path(__file__).resolve().parent.parent / "assets"
# Стили — переменными страницы чата: свои токены файл не везёт.
CSS: Final = compact_css((_ASSETS / "photo.css").read_text(encoding="utf-8"))

# Надписи фото (spec 0081 §8; решение — docs/founder-review/2026-11-11-pr996-request-photo.md):
# подпись своего фото в ленте и три отказа — статусом над строкой ввода, в переписку не
# пишутся. Кнопка — в странице (`page.CHAT_TEXTS["photo"]`): она видна до загрузки файла.
PHOTO_TEXTS: Final[dict[str, dict[str, str]]] = {
    "photo_alt": {"kk": "Сіздің фотоңыз", "ru": "Ваше фото", "en": "Your photo"},
    "photo_rejected": {
        "kk": "Бұл фотоны жіберу мүмкін болмады. Басқа суретті жіберіп көріңіз.",
        "ru": "Не получилось отправить это фото. Попробуйте другой снимок.",
        "en": "We couldn't send this photo. Please try another one.",
    },
    "photo_limit": {
        "kk": "Қатарынан тым көп фото жіберілді. Бірнеше минуттан кейін қайталап көріңіз.",
        "ru": "Слишком много фото подряд. Попробуйте через несколько минут.",
        "en": "Too many photos in a row. Please try again in a few minutes.",
    },
    "photo_failed": {
        "kk": "Фото жіберілмеді. Қайталап көріңіз.",
        "ru": "Фото не отправилось. Попробуйте ещё раз.",
        "en": "The photo didn't go through. Please try again.",
    },
}


def _json(data: object) -> str:
    """JSON внутри скрипта: `<` экранируется, как у JSON страницы чата (`page._json`)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


SCRIPT: Final = compact_js(
    (_ASSETS / "photo.js")
    .read_text(encoding="utf-8")
    .replace("__PHOTO_TEXTS__", _json(PHOTO_TEXTS))
    .replace("__PHOTO_CSS__", _json(CSS))
).encode()
# Маршрут — `photos/router.py`; его роутер подключён раньше чата, иначе путь съест страница
# `/{tenant_slug}/{room_number}`.
PATH: Final = "/g/static/photo.js"
VERSION: Final = hashlib.sha256(SCRIPT).hexdigest()[:12]
URL: Final = f"{PATH}?v={VERSION}"
# Загрузчик файла и кнопка камеры — в скрипт страницы чата, только где фото включены
# (`page.render(photos=True)`): у остальных страниц он не весит ничего.
LOADER: Final = compact_js(
    (_ASSETS / "photo_loader.js").read_text(encoding="utf-8").replace("__PHOTO_SRC__", URL)
)
