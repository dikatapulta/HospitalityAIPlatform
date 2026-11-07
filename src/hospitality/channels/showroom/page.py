"""HTML 3D-витрины: страница посетителя и экран кода доступа (spec 0069 §2.2, §5).

Один HTML без сборки; скрипты и стили — свои файлы (`assets.py`): CSP `default-src
'self'` встроенных не пускает. Разметку просмотрщика и панели рисует `showroom.js` по
JSON-блоку `#sr-config` — тот же скрипт работает и в кабинете (`staff_portal/showroom.py`).
Cookie, кроме доступа по коду, нет; аналитики нет; поисковикам страница закрыта до
публичного режима (PR г).
"""

from __future__ import annotations

import json
from html import escape
from typing import Any, Final

from hospitality.channels.showroom import assets
from hospitality.channels.showroom.texts import LANGUAGE_LABELS, LANGUAGES, TEXTS, Language
from hospitality.modules.showroom import api as showroom_api

HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin",
    "X-Robots-Tag": "noindex",
}


def config_json(config: dict[str, Any]) -> str:
    """JSON для `<script type="application/json">`: `<`, `>` и `&` экранированы —
    название отеля не закроет тег."""
    return (
        json.dumps(config, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def viewer_config(
    *, language: Language, scene_url: str, rooms_url: str, navigate_url: str
) -> dict[str, Any]:
    """Настройки `showroom.js` и помощника (`assistant.js`): язык, надписи и адреса —
    общие для посетителя и кабинета. `maxGroupRooms` — предел номеров в запросе группы:
    больше страница не подбирает."""
    return {
        "lang": language,
        "texts": TEXTS[language],
        "sceneUrl": scene_url,
        "roomsUrl": rooms_url,
        "navigateUrl": navigate_url,
        "viewerUrl": assets.url("viewer.js"),
        "maxGroupRooms": showroom_api.MAX_GROUP_ROOMS,
    }


def _shell(language: Language, title: str, slug: str, heading: str, body: str) -> str:
    """Шапка прототипа: надзаголовок, название отеля, подзаголовок, цифры сцены (их
    заполняет `showroom.js` в `[data-sr-stats]`) и переключатель языка ссылками."""
    texts = TEXTS[language]
    links = "".join(
        f'<a href="/showroom/{escape(slug)}?lang={code}"'
        f"{' aria-current="true"' if code == language else ''}>{LANGUAGE_LABELS[code]}</a>"
        for code in LANGUAGES
    )
    return (
        f'<!doctype html><html lang="{language}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        '<meta name="color-scheme" content="dark">'
        f"<title>{escape(title)}</title>"
        f'<link rel="stylesheet" href="{assets.url("showroom.css")}"></head>'
        '<body class="sr-page"><header class="sr-top"><div class="sr-ttl">'
        f'<p class="sr-eyebrow">{escape(texts["eyebrow"])}</p><h1>{escape(heading)}</h1>'
        f'<p class="sr-sub">{escape(texts["sub"])}</p></div><div class="sr-topr">'
        '<div class="sr-stats" data-sr-stats></div>'
        f'<nav class="sr-lang" aria-label="{escape(texts["langs_aria"])}">{links}</nav></div>'
        f"</header>{body}</body></html>"
    )


def render_page(*, slug: str, hotel_name: str, language: Language, blocks: str = "") -> str:
    """Страница посетителя: шапка, корень просмотрщика, настройки и скрипт.

    `blocks` — разметка блоков PR (б) и (в) (помощник, формы запроса): свои файлы и
    данные; в слоты `[data-sr-slot=…]` их переносят их же скрипты по `showroom:ready`."""
    texts = TEXTS[language]
    base = f"/showroom/{slug}"
    config = viewer_config(
        language=language,
        scene_url=f"{base}/scene",
        rooms_url=f"{base}/rooms",
        navigate_url=f"{base}/navigate",
    )
    body = (
        '<main class="sr-root" data-sr-root></main>'
        f"{blocks}"
        f'<script type="application/json" id="sr-config">{config_json(config)}</script>'
        f'<script type="module" src="{assets.url("showroom.js")}"></script>'
        f'<script type="module" src="{assets.url("assistant.js")}"></script>'
    )
    return _shell(language, texts["title"].format(hotel=hotel_name), slug, hotel_name, body)


def render_code_screen(*, slug: str, hotel_name: str, language: Language) -> str:
    """Экран «Введите код доступа» (§5): форма, ошибка и лимит — на языке страницы."""
    texts = TEXTS[language]
    body = (
        f'<form class="sr-access" data-sr-access data-endpoint="/showroom/{escape(slug)}/access"'
        f' data-wrong="{escape(texts["code_wrong"])}"'
        f' data-limited="{escape(texts["code_limited"])}">'
        f"<h1>{escape(texts['code_title'])}</h1>"
        '<input class="sr-input" type="password" name="code" autocomplete="off" required'
        f' minlength="8" maxlength="64" aria-label="{escape(texts["code_title"])}">'
        f'<button type="submit" class="sr-btn sr-btn-primary">{escape(texts["code_open"])}</button>'
        '<p class="sr-hint sr-error" data-sr-access-error role="alert" hidden></p></form>'
        f'<script src="{assets.url("access.js")}" defer></script>'
    )
    return _shell(language, texts["title"].format(hotel=hotel_name), slug, hotel_name, body)
