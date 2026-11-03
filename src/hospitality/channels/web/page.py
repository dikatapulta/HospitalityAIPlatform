"""Страница веб-чата и вход по листку — статический HTML (spec 0027 §3.4, P-1, R-3).

Без сборки и зависимостей: вторая экосистема (Next.js) ради одной страницы не
окупается — кабинет персонала пришёл к тому же выводу и рендерится server-side в
монолите (ADR-014). Стили и скрипт лежат файлами в `assets/` и вклеиваются один раз
при импорте, как у меню (spec 0038); вид — #746: раскладка «Небесный консьерж» на
подложке «Небо и золото», токены — общие для всех гостевых страниц (`styles.py`).
Slug и комнату страница читает из своего URL; сессия — в HttpOnly-cookie, JS
токена не видит.

Экран входа = экран согласия: кнопка «войти» и есть согласие на обработку ПД
(spec 0029). Текст — общий канон каналов (`channels/common/consent.py`, дословная
копия `docs/legal/consent-text.md`), во всех трёх языках: до входа язык гостя
неизвестен всегда. После входа надписи — на одном языке: выбор гостя в шапке,
иначе язык телефона. Подстановка — маркерами `__…__`, а не `str.format`: в HTML
полно фигурных скобок (CSS, JS).
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Final
from urllib.parse import quote

from hospitality.channels.common.consent import (
    CONSENT_LANGUAGES,
    CONSENT_VERSION,
    consent_button_label,
    consent_text,
)
from hospitality.channels.web.documents.texts import PAGE_TEXTS
from hospitality.channels.web.styles import (
    THEME_COLOR_META,
    TOKENS_CSS,
    compact_css,
    compact_js,
)
from hospitality.channels.web.wallpaper import wallpaper_css
from hospitality.platform.legal import privacy_policy_url

_ASSETS: Final = Path(__file__).resolve().parent / "assets"
_CSS: Final = compact_css(
    TOKENS_CSS + wallpaper_css() + (_ASSETS / "chat.css").read_text(encoding="utf-8")
)
# Черта «Новое» и счётчик во вкладке (#223) — вклеивается в скрипт чата.
_JS: Final = compact_js(
    (_ASSETS / "chat.js")
    .read_text(encoding="utf-8")
    .replace("__UNREAD_JS__", (_ASSETS / "unread.js").read_text(encoding="utf-8"))
)

# Надписи чата после входа — по языку (kk, ru, en); единственный их владелец.
# «Отправить», «печатает», «язык» — новые с #746 (показ основателю — docs/founder-review/).
CHAT_TEXTS: Final[dict[str, dict[str, str]]] = {
    "room": {"kk": "Бөлме", "ru": "Комната", "en": "Room"},
    "message": {"kk": "Хабарлама", "ru": "Сообщение", "en": "Message"},
    "send": {"kk": "Жіберу", "ru": "Отправить", "en": "Send"},
    "typing": {"kk": "Қонақүй жазып жатыр", "ru": "Отель печатает", "en": "Hotel is typing"},
    "language": {"kk": "Тіл", "ru": "Язык", "en": "Language"},
    "not_delivered": {
        "kk": "Жеткізілмеді, қайталап көріңіз",
        "ru": "Не доставлено, попробуйте ещё раз",
        "en": "Not delivered, try again",
    },
    "orders": {"kk": "Нөмірге тапсырыс", "ru": "Заказать в номер", "en": "Order to your room"},
    "table": {"kk": "Үстел брондау", "ru": "Забронировать столик", "en": "Book a table"},
    "greeting": {
        "kk": "Қосылдыңыз. Қалай көмектесе аламыз?",
        "ru": "Вы подключены. Чем можем помочь?",
        "en": "You're connected. How can we help?",
    },
}
# Запасная ошибка входа — на экране входа, поэтому на трёх языках.
_GATE_ERROR: Final = "Қате, қайталап көріңіз · Ошибка, попробуйте ещё раз · Error. Try again"

_HEAD: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
__THEME__
<link rel="icon" href="__FAVICON__">
<title>__HOTEL__</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="wall" aria-hidden="true"></div>
<div class="app">
<header class="topbar">
  <span class="mono" aria-hidden="true">__INITIAL__</span>
  <div class="title glass"><b>__HOTEL__</b><span id="room-line">__SUBTITLE__</span></div>
  __LANGS__
</header>
"""

_GATE: Final = """<section class="gate" id="gate"__GATE_HIDDEN__>
  <div class="card">
    <h1>Қонақүй чаты
Чат с отелем
Hotel guest chat</h1>
    <p class="lead" id="gate-lead">__LEAD__</p>
    __CODE_FIELD__
    <div class="consent">__CONSENT_TEXT__</div>
    <button type="button" class="cta" id="enter">__CONSENT_BUTTON__</button>
    <p class="version">__CONSENT_VERSION__</p>
    __BIND_ERROR__
  </div>
</section>
"""

_CODE_FIELD: Final = """<input id="code" autocomplete="one-time-code" inputmode="numeric"
           aria-labelledby="gate-lead" placeholder="482-913" maxlength="12">
    <p class="field-error" id="gate-error" role="alert" hidden></p>"""

_PAGE: Final = (
    _HEAD
    + """<main id="log" role="log" aria-live="polite" hidden></main>
"""
    + _GATE
    + """<form id="composer" hidden>
  <div class="actions">__ORDERS_LINK____TABLE_LINK__</div>
  <p class="pause glass" id="chat-pause" role="status" hidden></p>
  <div class="row">
    __DOCUMENTS_CLIP__
    <div class="field glass"><input id="text" autocomplete="off" enterkeyhint="send"></div>
    <button class="circle send" id="send" type="submit"><svg viewBox="0 0 24 24" fill="none"
      stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
  </div>
  __DOCUMENTS_HINT__
</form>
</div>
<script id="chat-texts" type="application/json">__TEXTS__</script>
<script>
__JS__
</script>
</body>
</html>
"""
)

_LANGS: Final = (
    '<div class="langs glass" id="langs" role="group" hidden>'
    '<button type="button" data-lang="kk">ҚАЗ</button>'
    '<button type="button" data-lang="ru">РУС</button>'
    '<button type="button" data-lang="en">ENG</button></div>'
)

_LEAD: Final = (
    "Тіркелу карточкасындағы кодты енгізіңіз.\n"
    "Введите код заселения с карточки, выданной на ресепшене.\n"
    "Enter the check-in code from your reception card."
)
_BIND_LEAD: Final = "Қонақүй чатына қосылу.\nПодключение к чату отеля.\nConnect to the hotel chat."


def _icon(path: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )


def _action(link_id: str, path: str, text_key: str, icon: str) -> str:
    """Плашка над строкой ввода; видна после входа (JS `toChat`), подпись — по языку."""
    label = html.escape(CHAT_TEXTS[text_key]["ru"])
    return (
        f'<a class="action glass" id="{link_id}" data-path="{path}" href="#" hidden>'
        f'<span class="ico">{_icon(icon)}</span><span data-t="{text_key}">{label}</span></a>'
    )


# «Заказать в номер» (spec 0038 §5) — только у отеля с включённым room service.
_ORDERS_LINK: Final = _action(
    "orders-link", "menu", "orders", '<path d="M3 17h18M5 17a7 7 0 0 1 14 0M12 10V8"/>'
)
# «Забронировать столик» (spec 0064 §5) — только у отеля с включённой бронью.
_TABLE_LINK: Final = _action(
    "table-link",
    "table",
    "table",
    '<path d="M7 3v8M5 3v4a2 2 0 0 0 4 0V3M7 11v10M17 21V3c-2 1-3 4-3 8h3"/>',
)

# Скрепка формы реквизитов для АВР (spec 0049 Р-7) — только там, где приём
# включён; видна после входа. Подписи трёх языков — JSON рядом, язык выбирает скрипт.
_DOCUMENTS_CLIP: Final = (
    '<a class="circle glass" id="clip" href="#" hidden>'
    + _icon(
        '<path d="M21 11.5l-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8L13.2 3.7a3.5 3.5 0 0 1 5 5L9.7 '
        '17.2a1.5 1.5 0 0 1-2.1-2.1L15.5 7"/>'
    )
    + "</a>"
)


def _json(data: object) -> str:
    """JSON для `<script type="application/json">`: `<` экранируется."""
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def _documents_hint() -> str:
    data = {
        "label": dict(zip(("ru", "kk", "en"), PAGE_TEXTS["clip_label"], strict=True)),
        "hint": dict(zip(("ru", "kk", "en"), PAGE_TEXTS["clip_hint"], strict=True)),
    }
    return (
        '<p class="clip-hint glass" id="clip-hint" hidden></p>'
        f'<script id="clip-texts" type="application/json">{_json(data)}</script>'
    )


def _consent_html() -> str:
    """Канон согласия всех трёх языков абзацами; ссылка на политику — живая."""
    url = html.escape(privacy_policy_url())
    link = f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
    return "".join(
        f"<p>{html.escape(consent_text(code)).replace(url, link)}</p>" for code in CONSENT_LANGUAGES
    )


def _favicon(initial: str) -> str:
    """Знак отеля для вкладки: буква на небесном круге с золотым ободком."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<circle cx="32" cy="32" r="29" fill="#1f6fb2" stroke="#c9a24a" stroke-width="4"/>'
        '<text x="32" y="43" font-family="system-ui,sans-serif" font-size="32" font-weight="700" '
        f'fill="#fff" text-anchor="middle">{html.escape(initial)}</text></svg>'
    )
    return "data:image/svg+xml," + quote(svg)


def _shell(template: str, *, hotel_name: str, subtitle: str, langs: str, gate_hidden: bool) -> str:
    """Общее у чата и страницы с листка: шапка с отелем, подложка, экран согласия."""
    name = hotel_name.strip() or "Hotel"
    initial = name[0].upper()
    return (
        template.replace("__THEME__", THEME_COLOR_META)
        .replace("__FAVICON__", _favicon(initial))
        .replace("__CSS__", _CSS)
        .replace("__HOTEL__", html.escape(name))
        .replace("__INITIAL__", html.escape(initial))
        .replace("__SUBTITLE__", subtitle)
        .replace("__LANGS__", langs)
        .replace("__GATE_HIDDEN__", " hidden" if gate_hidden else "")
        .replace("__CONSENT_TEXT__", _consent_html())
        .replace("__CONSENT_BUTTON__", html.escape(consent_button_label(None)))
        .replace("__CONSENT_VERSION__", f"Келісім · Согласие · Consent {CONSENT_VERSION}")
    )


def render(
    *,
    hotel_name: str = "",
    room_service: bool = False,
    documents: bool = False,
    table_booking: bool = False,
) -> str:
    """HTML страницы чата: название отеля — в шапке и на вкладке, кнопки — по конфигу."""
    texts = {**CHAT_TEXTS, "gate_error": _GATE_ERROR}
    return (
        _shell(_PAGE, hotel_name=hotel_name, subtitle="", langs=_LANGS, gate_hidden=True)
        .replace("__LEAD__", html.escape(_LEAD))
        .replace("__CODE_FIELD__", _CODE_FIELD)
        .replace("__BIND_ERROR__", "")
        .replace("__ORDERS_LINK__", _ORDERS_LINK if room_service else "")
        .replace("__TABLE_LINK__", _TABLE_LINK if table_booking else "")
        .replace("__DOCUMENTS_CLIP__", _DOCUMENTS_CLIP if documents else "")
        .replace("__DOCUMENTS_HINT__", _documents_hint() if documents else "")
        .replace("__TEXTS__", _json(texts))
        .replace("__JS__", _JS)
    )


# Страница ссылки привязки с листка (spec 0033 §6): тот же экран согласия, что у
# ввода кода, но вместо поля кода — одна кнопка. Сессия рождается ТОЛЬКО по нажатию
# (POST …/session): открытие страницы — не согласие; после успеха гость уезжает в
# обычный чат (/g/{slug}/{room}).
_BIND_PAGE: Final = (
    _HEAD
    + _GATE
    + """</div>
<script>
"use strict";
const button = document.getElementById("enter");
const error = document.getElementById("bind-error");
button.addEventListener("click", async () => {
  button.disabled = true; button.setAttribute("aria-busy", "true");
  try {
    let data = null;
    let ok = false;
    try {
      const response = await fetch(location.pathname + "/session", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
      ok = response.ok;
      try { data = await response.json(); } catch (e) { /* не-JSON — ниже */ }
    } catch (e) { /* сети нет — ниже */ }
    if (ok && data && data.chat_url) { location.href = data.chat_url; return; }
    error.textContent = (data && data.error && data.error.message) || "__GATE_ERROR__";
    error.hidden = false;
  } finally {
    button.disabled = false; button.removeAttribute("aria-busy");
  }
});
</script>
</body>
</html>
"""
)


def render_bind(*, hotel_name: str = "") -> str:
    """HTML страницы ссылки привязки с листка (spec 0033 §6)."""
    return (
        _shell(_BIND_PAGE, hotel_name=hotel_name, subtitle="", langs="", gate_hidden=False)
        .replace("__LEAD__", html.escape(_BIND_LEAD))
        .replace("__CODE_FIELD__", "")
        .replace(
            "__BIND_ERROR__", '<p class="field-error" id="bind-error" role="alert" hidden></p>'
        )
        .replace("__GATE_ERROR__", _GATE_ERROR)
    )
