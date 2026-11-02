"""HTML страницы «Забронировать столик» — один статический документ (spec 0064, P-1).

Канон страницы «Заказать в номер» (`menu/page.py`): без сборки и зависимостей.
Стили — общие с меню (`menu.css`: токены, шапка, карточки, нижняя полоса) плюс свои
поля формы (`table.css`), скрипт — `table.js`; вклеиваются один раз при импорте,
надписи трёх языков — JSON'ом (`texts.py`). Данных гостя в HTML нет: рестораны,
комнату и брони скрипт берёт JSON'ом из `…/table/data` за сессией.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from hospitality.channels.web.menu.page import CSS as MENU_CSS
from hospitality.channels.web.table.texts import page_texts_json

_ASSETS: Final = Path(__file__).resolve().parent / "assets"
CSS: Final = MENU_CSS + "\n" + (_ASSETS / "table.css").read_text(encoding="utf-8")
JS: Final = (_ASSETS / "table.js").read_text(encoding="utf-8")

_SHELL: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#f5f7f4" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1310" media="(prefers-color-scheme: dark)">
<title>Book a table</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="app" id="app"></div>
<div class="bottom-bar" id="bar" hidden><div class="bottom-bar-inner" id="bar-inner"></div></div>
<div class="scrim" id="scrim" hidden></div>
<div class="sheet" id="sheet" role="dialog" aria-modal="true"
     aria-labelledby="sheet-title" hidden></div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script id="table-texts" type="application/json">__TEXTS__</script>
<script>
__JS__
</script>
</body>
</html>
"""

# `<` в JSON экранируется: данные живут внутри `<script>`.
_TEXTS: Final = json.dumps(page_texts_json(), ensure_ascii=False).replace("<", "\\u003c")
_PAGE: Final = _SHELL.replace("__CSS__", CSS).replace("__JS__", JS).replace("__TEXTS__", _TEXTS)


def render() -> str:
    """HTML страницы; одинаков для всех гостей — данные приходят JSON'ом."""
    return _PAGE
