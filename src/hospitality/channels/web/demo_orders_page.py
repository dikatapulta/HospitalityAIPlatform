"""Страница «Заказать в номер» веб-чата — демо к презентации (issue #438, дизайн — #440).

Кликабельный макет на выдуманных данных: ни записи в БД, ни вызовов модели,
ни запросов к нашему серверу после загрузки — корзины, оформление и шаги
статуса живут в памяти страницы. Отдаёт её маршрут `GET /g/{slug}/{room}/orders`
(`router.py`) только за живой гостевой сессией и при `ENABLE_DEMO_ORDERS`.

Один HTML без сборки, как страница чата (`page.py`): стили и скрипт лежат
файлами в `demo_orders_assets/` и вклеиваются при импорте, данные — каталог
(`channels/common/demo_orders.py`), витрина (`demo_orders_extras.py`) и надписи
(`demo_orders_texts.py`) — JSON'ом через маркер `__DEMO_DATA__`. Шрифты Onest и
Unbounded — с Google Fonts, как в прототипе spec 0038: демо живёт только на
staging, где гостей нет; настоящему room service шрифты — своими файлами или
системные. Язык — kk/ru/en по браузеру, переключатель в шапке. Удаляется PR'ом
настоящего room service (spec 0038).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from hospitality.channels.common.demo_orders import DEMO_CATALOG, DEMO_ITEMS, DemoSection
from hospitality.channels.web.demo_orders_extras import ITEM_LOOKS, SECTION_LOOKS
from hospitality.channels.web.demo_orders_texts import DEMO_TEXTS

_ASSETS: Final = Path(__file__).resolve().parent / "demo_orders_assets"
CSS: Final = (_ASSETS / "page.css").read_text(encoding="utf-8")
JS: Final = (_ASSETS / "page.js").read_text(encoding="utf-8")

_SHELL: Final = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="theme-color" content="#f6f7f5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d1210" media="(prefers-color-scheme: dark)">
<title>Room service · Demo</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&family=Unbounded:wght@600;700&display=swap">
<style>
__CSS__
</style>
</head>
<body>
<div class="ribbon"><b class="ribbon-tag" data-t="demo"></b><span data-t="demo_note"></span></div>
<header class="top" id="top">
  <div class="top-inner">
    <button class="icon-btn" id="back" type="button"></button>
    <div class="top-title">
      <b data-t="title"></b>
      <span><span id="room"></span><i class="demo-pill" data-t="demo"></i></span>
    </div>
    <div class="langs" role="group" aria-label="Language">
      <button type="button" data-lang="kk">ҚАЗ</button>
      <button type="button" data-lang="ru">РУС</button>
      <button type="button" data-lang="en">ENG</button>
    </div>
  </div>
</header>
<div class="wrap">
  <div class="seg" id="seg" role="tablist" data-active="restaurant">
    <i class="seg-pill" aria-hidden="true"></i>
    <button type="button" role="tab" data-seg="restaurant">🍽 <span></span></button>
    <button type="button" role="tab" data-seg="shop">🛍 <span></span></button>
  </div>
  <main id="view"></main>
</div>
<div class="bar" id="bar" hidden></div>
<div class="scrim" id="scrim" hidden></div>
<section class="sheet" id="sheet" role="dialog" aria-modal="true" hidden></section>
<script id="demo-data" type="application/json">__DEMO_DATA__</script>
<script>
__JS__
</script>
</body>
</html>
"""

# Стили и скрипт — свои файлы, вклеиваются один раз; данные гостя — последними,
# чтобы их содержимое уже не проходило ни через одну замену маркеров.
_PAGE: Final = _SHELL.replace("__CSS__", CSS).replace("__JS__", JS)


def _item(key: str) -> dict[str, Any]:
    item, look = DEMO_ITEMS[key], ITEM_LOOKS[key]
    return {
        "emoji": item.emoji,
        "price": item.price_kzt,
        "name": asdict(item.name),
        "desc": asdict(look.desc),
        "meta": asdict(look.meta) if look.meta else None,
        "badge": look.badge,
        "colors": list(look.colors),
    }


def _section(section: DemoSection) -> dict[str, Any]:
    look = SECTION_LOOKS[section]
    return {
        "key": section.value,
        "art": list(look.art),
        "popular": list(look.popular),
        "upsell": list(look.upsell),
        "eta_steps": list(look.eta_steps),
        "categories": [
            {"title": asdict(category.title), "items": [item.key for item in category.items]}
            for category in DEMO_CATALOG
            if category.section is section
        ],
    }


def _payload(room_number: str) -> str:
    """Данные страницы одной JSON-строкой: комната из сессии, надписи, витрина.

    `<` экранируется: JSON живёт внутри `<script>`, и `</script>` в данных
    (комнату вносит персонал) закрыл бы его раньше времени.
    """
    data: dict[str, Any] = {
        "room": room_number,
        "texts": {key: asdict(text) for key, text in DEMO_TEXTS.items()},
        "items": {key: _item(key) for key in DEMO_ITEMS},
        "sections": [_section(section) for section in DemoSection],
    }
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def render(room_number: str) -> str:
    """HTML демо-страницы для комнаты гостевой сессии."""
    return _PAGE.replace("__DEMO_DATA__", _payload(room_number))
