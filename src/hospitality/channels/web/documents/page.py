"""HTML формы «Реквизиты для закрывающих документов» — один статический документ (spec 0049).

Канон страницы меню (`channels/web/menu/page.py`, spec 0038): без сборки и
зависимостей, стили и скрипт лежат файлами в `assets/` и вклеиваются один раз
при импорте, надписи трёх языков — JSON'ом (`texts.py`). Данных гостя в HTML
нет: номер и даты скрипт берёт JSON'ом из `…/documents/data` за сессией.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from hospitality.channels.web.documents.texts import page_texts_json
from hospitality.channels.web.styles import THEME_COLOR_META, TOKENS_CSS

_ASSETS: Final = Path(__file__).resolve().parent / "assets"
# Токены — общие для всех гостевых страниц (#746), свои стили — поверх.
CSS: Final = TOKENS_CSS + (_ASSETS / "documents.css").read_text(encoding="utf-8")
JS: Final = (_ASSETS / "documents.js").read_text(encoding="utf-8")

_SHELL: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
__THEME__
<title>Documents</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="app" id="app"></div>
<div class="bottom-bar" id="bar" hidden><div class="bottom-bar-inner" id="bar-inner"></div></div>
<script id="documents-texts" type="application/json">__TEXTS__</script>
<script>
__JS__
</script>
</body>
</html>
"""

# `<` в JSON экранируется: данные живут внутри `<script>`.
_TEXTS: Final = json.dumps(page_texts_json(), ensure_ascii=False).replace("<", "\\u003c")
_PAGE: Final = (
    _SHELL.replace("__THEME__", THEME_COLOR_META)
    .replace("__CSS__", CSS)
    .replace("__JS__", JS)
    .replace("__TEXTS__", _TEXTS)
)


def render() -> str:
    """HTML страницы; одинаков для всех гостей — данные приходят JSON'ом."""
    return _PAGE
