"""Страница веб-чата и вход по листку — статический HTML (spec 0027 §3.4, P-1, R-3).

Без сборки и зависимостей: вторая экосистема (Next.js) ради одной страницы не
окупается — кабинет персонала пришёл к тому же выводу и рендерится server-side в
монолите (ADR-014). Стили и скрипт лежат файлами в `assets/` и вклеиваются один раз
при импорте, как у меню (spec 0038); вид — #746: раскладка B+C (решение в #746 от
03.11) — шапка и строка ввода плавают над лентой, знак отеля справа и у его ответов,
пустой чат — приветствие с плитками; подложка «Небо и золото», токены — общие для
всех гостевых страниц (`styles.py`); цвет подложки, узор и знак отеля выбирает
менеджер (spec 0066, `look.py`). Шапка и строка ввода стоят на месте, прокручивается
только `.scroller` (spec 0068 §2.3); манифест своей комнаты — «приложение» с экрана «Домой».
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
from hospitality.channels.web import homescreen, outage, wallpaper
from hospitality.channels.web.documents.texts import PAGE_TEXTS
from hospitality.channels.web.feedback import widget as feedback_widget
from hospitality.channels.web.look import DARK_MEDIA, look_css
from hospitality.channels.web.styles import (
    THEME_COLOR_META,
    TOKENS_CSS,
    compact_css,
    compact_js,
    drop_unused_tokens,
    tight_html,
)
from hospitality.platform.guest_chat_look import GuestChatLook
from hospitality.platform.legal import privacy_policy_url

_ASSETS: Final = Path(__file__).resolve().parent / "assets"
_CSS: Final = drop_unused_tokens(
    compact_css(TOKENS_CSS + (_ASSETS / "chat.css").read_text(encoding="utf-8"))
)
# Черта «Новое» и счётчик во вкладке (#223) — вклеивается в скрипт чата; виджет оценки
# (spec 0076 §5) — адресом своего файла, грузит его скрипт чата.
_JS: Final = compact_js(
    (_ASSETS / "chat.js")
    .read_text(encoding="utf-8")
    .replace("__UNREAD_JS__", (_ASSETS / "unread.js").read_text(encoding="utf-8"))
    .replace("__FB_SRC__", feedback_widget.URL)
)

# Надписи чата после входа — по языку (kk, ru, en); единственный их владелец.
# «Отправить», «печатает», «язык», приветствие и плитки пустого чата, строка комнаты —
# с #746 (решение по раскладке в #746 и docs/founder-review/). В «room» `{}` — номер.
CHAT_TEXTS: Final[dict[str, dict[str, str]]] = {
    "room": {"kk": "{}-бөлме", "ru": "Номер {}", "en": "Room {}"},
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
    "welcome": {"kk": "Қош келдіңіз!", "ru": "Здравствуйте!", "en": "Welcome!"},
    "pick": {
        "kk": "Қажетін жазыңыз немесе таңдаңыз:",
        "ru": "Напишите, что нужно, или выберите:",
        "en": "Type what you need, or pick one:",
    },
    "towels": {"kk": "Сүлгі керек", "ru": "Нужны полотенца", "en": "I need towels"},
    "breakfast": {
        "kk": "Таңғы ас қашан?",
        "ru": "Во сколько завтрак?",
        "en": "What time is breakfast?",
    },
}
# Запасная ошибка входа — на экране входа, поэтому на трёх языках.
_GATE_ERROR: Final = "Қате, қайталап көріңіз · Ошибка, попробуйте ещё раз · Error. Try again"
# Обои (#773) — отдельным запросом после первой отрисовки (`load`): страница их не ждёт и
# не тяжелеет. Картинка проявляется, когда загрузилась; не загрузилась — остаётся градиент.
_WALL_JS: Final = (
    '<script>addEventListener("load",()=>{const w=document.querySelector(".wall img");'
    'w.onload=()=>w.classList.add("on");w.src=w.dataset.src})</script>'
)

_HEAD: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
__THEME__
__APP__
<link rel="icon" href="__FAVICON__">
<title>__HOTEL__</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="wall" aria-hidden="true"><img alt="" data-src="__WALL__"></div>
__WALL_JS__
<div class="app">
<header class="topbar">
  __LANGS__
  <div class="title glass"><b>__HOTEL__</b><span id="room-line">__SUBTITLE__</span></div>
  <span class="mono" id="mono" aria-hidden="true">__MARK__</span>
</header>
<div class="scroller" id="scroller">
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

# Поле комнаты (spec 0068 §2.2): заполнено комнатой из адреса, гость может исправить — внутри
# приложения с экрана «Домой» QR не отсканировать, а иконка могла остаться от прежней комнаты.
_CODE_FIELD: Final = """<label for="room">Бөлме нөмірі · Номер комнаты · Room number</label>
    <input id="room" value="__ROOM__" autocomplete="off" autocapitalize="characters"
           enterkeyhint="next" maxlength="20">
    <label for="code">Тіркелу коды · Код заселения · Check-in code</label>
    <input id="code" autocomplete="one-time-code" inputmode="numeric" enterkeyhint="go"
           placeholder="482-913" maxlength="12">
    <p class="field-error" id="gate-error" role="alert" hidden></p>"""

_PAGE: Final = tight_html(
    _HEAD
    + """<section class="welcome glass" id="welcome" hidden>
  <p class="big" data-t="welcome"></p><p data-t="pick"></p>
  <div class="tiles">__TILES__</div>
</section>
<main id="log" role="log" aria-live="polite" hidden></main>
"""
    + _GATE
    + """</div>
<form id="composer" hidden>
  <div class="actions">__ORDERS_LINK____TABLE_LINK__</div>
  <p class="pause glass" id="chat-pause" role="status" hidden></p>
  <div class="row">
    __DOCUMENTS_CLIP__
    <div class="field glass"><input id="text" autocomplete="off" enterkeyhint="send"></div>
    <button class="circle send" id="send" type="submit"><svg viewBox="0 0 24 24"
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

# Язык — одна пилюля с текущим языком, касание раскрывает выбор; до входа выбора нет
# (экран входа на трёх языках), пилюля выключена. Кнопки шапки и плиток стоят вне формы,
# поэтому `type="button"` им не нужен.
_LANGS: Final = (
    '<div class="lang" id="langs"><button class="pill glass" id="lang" aria-expanded="false" '
    'disabled></button><div class="lang-menu glass" id="lang-menu" hidden>'
    '<button data-lang="kk">ҚАЗ</button><button data-lang="ru">РУС</button>'
    '<button data-lang="en">ENG</button></div></div>'
)

_LEAD: Final = (
    "Бөлме нөмірін және тіркелу карточкасындағы кодты енгізіңіз.\n"
    "Введите номер комнаты и код заселения с карточки, выданной на ресепшене.\n"
    "Enter your room number and the check-in code from your reception card."
)
_BIND_LEAD: Final = "Қонақүй чатына қосылу.\nПодключение к чату отеля.\nConnect to the hotel chat."


def _icon(path: str) -> str:
    """Иконка линиями; толщину, цвет и скругления задаёт `chat.css` (правило `svg`)."""
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{path}</svg>'


def _action(link_id: str, path: str, text_key: str, icon: str) -> str:
    """Плашка над строкой ввода; видна после входа (JS `toChat`), подпись — по языку."""
    label = html.escape(CHAT_TEXTS[text_key]["ru"])
    return (
        f'<a class="action glass" id="{link_id}" data-path="{path}" href="#" hidden>'
        f'<span class="ico">{_icon(icon)}</span><span data-t="{text_key}">{label}</span></a>'
    )


_CLOCHE: Final = '<path d="M3 17h18M5 17a7 7 0 0 1 14 0M12 10V8"/>'
# «Заказать в номер» (spec 0038 §5) — только у отеля с включённым room service.
_ORDERS_LINK: Final = _action("orders-link", "menu", "orders", _CLOCHE)
# «Забронировать столик» (spec 0064 §5) — только у отеля с включённой бронью.
_TABLE_LINK: Final = _action(
    "table-link",
    "table",
    "table",
    '<path d="M7 3v8M5 3v4a2 2 0 0 0 4 0V3M7 11v10M17 21V3c-2 1-3 4-3 8h3"/>',
)


def _tile(key: str, icon: str) -> str:
    """Плитка пустого чата (#746): вставляет текст в поле, отправляет гость сам.

    Подпись ставит скрипт на языке гостя: блок скрыт, пока скрипт не решит, что чат пуст.
    """
    return (
        f'<button class="tile" data-fill="{key}">{_icon(icon)}<span data-t="{key}"></span></button>'
    )


_TILES: Final = _tile("towels", '<path d="M5 5h14v5H5zM5 14h14v5H5z"/>') + _tile(
    "breakfast", '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'
)
# Третья плитка — меню room service, только у отеля, где он включён.
_ORDERS_TILE: Final = (
    '<a class="tile wide" data-path="menu" href="#">'
    f'{_icon(_CLOCHE)}<span data-t="orders"></span></a>'
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
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _documents_hint() -> str:
    data = {
        "label": dict(zip(("ru", "kk", "en"), PAGE_TEXTS["clip_label"], strict=True)),
        "hint": dict(zip(("ru", "kk", "en"), PAGE_TEXTS["clip_hint"], strict=True)),
    }
    return (
        '<p class="clip-hint glass" id="clip-hint" hidden></p>'
        f'<script id="clip-texts" type="application/json">{_json(data)}</script>'
    )


def _consent_html(tenant_slug: str) -> str:
    """Канон согласия всех трёх языков абзацами; ссылка на политику — живая.

    Ссылка — на страницу политики отеля (spec 0029 §11); без slug (тесты вида
    страницы) — на общую.
    """
    slug = tenant_slug or None
    url = html.escape(privacy_policy_url(tenant_slug=slug))
    link = f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
    return "".join(
        f"<p>{html.escape(consent_text(code, tenant_slug=slug)).replace(url, link)}</p>"
        for code in CONSENT_LANGUAGES
    )


def _favicon(initial: str) -> str:
    """Знак отеля для вкладки: буква на небесном круге с золотым ободком."""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        "<circle cx='32' cy='32' r='29' fill='#1f6fb2' stroke='#c9a24a' stroke-width='4'/>"
        "<text x='32' y='43' font-family='system-ui,sans-serif' font-size='32' font-weight='700' "
        f"fill='#fff' text-anchor='middle'>{html.escape(initial)}</text></svg>"
    )
    # Кодируется только то, что ломает URI или атрибут (`#`, `<`, `>`, `&`, `"`) — вес страницы.
    return "data:image/svg+xml," + quote(svg, safe=" =:/,.-'()")


def _shell(
    template: str,
    *,
    hotel_name: str,
    subtitle: str,
    langs: str,
    gate_hidden: bool,
    look: GuestChatLook | None,
    logo_url: str | None,
    tenant_slug: str = "",
) -> str:
    """Общее у чата и страницы с листка: шапка с отелем, подложка, экран согласия.

    Вид — выбор отеля (spec 0066 §5): `look` красит подложку, `logo_url` ставит знак
    в кружок шапки и во вкладку; без них — буква названия, как у #746. `tenant_slug` —
    адрес отеля в ссылке на политику (spec 0029 §11).
    """
    name = hotel_name.strip() or "Hotel"
    initial = name[0].upper()
    mark = f'<img src="{html.escape(logo_url)}" alt="">' if logo_url else html.escape(initial)
    return (
        template.replace("__THEME__", THEME_COLOR_META)
        .replace("__FAVICON__", html.escape(logo_url) if logo_url else _favicon(initial))
        .replace("__CSS__", _CSS + look_css(look or GuestChatLook()))
        .replace("__WALL_JS__", _WALL_JS)
        .replace("__WALL__", wallpaper.URL)
        .replace("__HOTEL__", html.escape(name))
        .replace("__MARK__", mark)
        .replace("__SUBTITLE__", subtitle)
        .replace("__LANGS__", langs)
        .replace("__GATE_HIDDEN__", " hidden" if gate_hidden else "")
        .replace("__CONSENT_TEXT__", _consent_html(tenant_slug))
        .replace("__CONSENT_BUTTON__", html.escape(consent_button_label(None)))
        .replace("__CONSENT_VERSION__", f"Келісім · Согласие · Consent {CONSENT_VERSION}")
    )


def render(
    *,
    hotel_name: str = "",
    tenant_slug: str = "",
    room_number: str = "",
    room_service: bool = False,
    documents: bool = False,
    table_booking: bool = False,
    look: GuestChatLook | None = None,
    logo_url: str | None = None,
    reception_room_dial: str | None = None,
) -> str:
    """HTML страницы чата: название отеля — в шапке и на вкладке, кнопки — по конфигу.

    `tenant_slug` и `room_number` — из адреса страницы: манифест своей комнаты и поле комнаты
    на экране входа (spec 0068); без slug (тесты вида страницы) ссылок на манифест нет.
    `reception_room_dial` страница держит у себя с загрузки: когда чат перестанет отвечать,
    прочитать его будет неоткуда (#991).
    """
    texts = {
        **CHAT_TEXTS,
        "gate_error": _GATE_ERROR,
        "call": outage.call_reception(reception_room_dial),
    }
    app_links = (
        homescreen.head_links(
            hotel_name=hotel_name, tenant_slug=tenant_slug, room_number=room_number
        )
        if tenant_slug
        else ""
    )
    return (
        _shell(
            _PAGE,
            hotel_name=hotel_name,
            subtitle="",
            langs=_LANGS,
            gate_hidden=True,
            look=look,
            logo_url=logo_url,
            tenant_slug=tenant_slug,
        )
        .replace("__TILES__", _TILES + (_ORDERS_TILE if room_service else ""))
        .replace("__LEAD__", html.escape(_LEAD))
        .replace("__CODE_FIELD__", _CODE_FIELD)
        .replace("__BIND_ERROR__", "")
        .replace("__ORDERS_LINK__", _ORDERS_LINK if room_service else "")
        .replace("__TABLE_LINK__", _TABLE_LINK if table_booking else "")
        .replace("__DOCUMENTS_CLIP__", _DOCUMENTS_CLIP if documents else "")
        .replace("__DOCUMENTS_HINT__", _documents_hint() if documents else "")
        .replace("__TEXTS__", _json(texts))
        .replace("__JS__", _JS)
        # Комната из адреса — в конце: её задаёт кто угодно, маркеры выше она не зацепит.
        .replace("__APP__", app_links)
        .replace("__ROOM__", html.escape(room_number))
    )


# Предпросмотр кабинета (spec 0066 §6): та же шапка и подложка, лента-образец, без скрипта.
_PREVIEW: Final = tight_html(
    _HEAD
    + """<main id="log"><div class="day">__DAY__</div>
<div class="msg inbound">__GUEST__<time>10:24</time></div>
<div class="msg outbound">__REPLY__<time>10:25</time><span class="ava">__MARK__</span></div></main>
</div><form id="composer"><div class="row"><div class="field glass">
<input id="text" placeholder="__PLACEHOLDER__" disabled></div>
<span class="circle send"><svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6"/></svg></span>
</div></form></div></body></html>"""
)


def render_preview(
    *,
    hotel_name: str,
    look: GuestChatLook,
    logo_url: str | None,
    night: bool,
    sample: tuple[str, str, str, str],
) -> str:
    """Телефон предпросмотра «Вид гостевого чата»: стили — страницы гостя, тема — по `night`.

    `sample` — подписи образца: строка комнаты, плашка дня, реплика гостя, ответ отеля;
    их владелец — кабинет. Тёмная тема включается подменой медиазапроса: у iframe
    своей темы нет, а показать нужно и день, и ночь рядом.
    """
    room, day, guest, reply = (html.escape(text) for text in sample)
    pill = '<div class="lang"><button class="pill glass" disabled>РУС</button></div>'
    page = _shell(
        _PREVIEW.replace("__DAY__", day)
        .replace("__GUEST__", guest)
        .replace("__REPLY__", reply)
        .replace("__PLACEHOLDER__", html.escape(CHAT_TEXTS["message"]["ru"]))
        .replace("__APP__", ""),
        hotel_name=hotel_name,
        subtitle=room,
        langs=pill,
        gate_hidden=True,
        look=look,
        logo_url=logo_url,
    )
    # Скриптов в предпросмотре нет (CSP), поэтому обои — сразу, без ожидания `load`.
    page = page.replace(_WALL_JS, "").replace('alt="" data-src=', 'alt="" class="on" src=')
    return page.replace(DARK_MEDIA, "@media all" if night else "@media not all")


# Страница ссылки привязки с листка (spec 0033 §6): тот же экран согласия, что у
# ввода кода, но вместо поля кода — одна кнопка. Сессия рождается ТОЛЬКО по нажатию
# (POST …/session): открытие страницы — не согласие; после успеха гость уезжает в
# обычный чат (/g/{slug}/{room}).
_BIND_PAGE: Final = (
    tight_html(_HEAD + _GATE)
    + """</div></div>
<script>
"use strict";
const button = document.getElementById("enter");
const error = document.getElementById("bind-error");
button.addEventListener("click", async () => {
  button.disabled = true; button.setAttribute("aria-busy", "true");
  try {
    let data = null;
    let ok = false;
    let down = true;
    try {
      const response = await fetch(location.pathname + "/session", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
      ok = response.ok;
      down = response.status >= 500;
      try { data = await response.json(); } catch (e) { /* не-JSON — ниже */ }
    } catch (e) { /* сети нет — ниже */ }
    if (ok && data && data.chat_url) { location.href = data.chat_url; return; }
    // 5xx и обрыв сети (#991): не «Internal server error», а куда идти.
    const message = (data && data.error && data.error.message) || "__GATE_ERROR__";
    error.textContent = down ? __GATE_DOWN__ : message;
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


def render_bind(
    *,
    hotel_name: str = "",
    tenant_slug: str = "",
    look: GuestChatLook | None = None,
    logo_url: str | None = None,
    reception_room_dial: str | None = None,
) -> str:
    """HTML страницы ссылки привязки с листка (spec 0033 §6); вид — как у чата."""
    return (
        _shell(
            _BIND_PAGE,
            hotel_name=hotel_name,
            subtitle="",
            langs="",
            gate_hidden=False,
            look=look,
            logo_url=logo_url,
            tenant_slug=tenant_slug,
        )
        .replace("__APP__", "")
        .replace("__LEAD__", html.escape(_BIND_LEAD))
        .replace("__CODE_FIELD__", "")
        .replace(
            "__BIND_ERROR__", '<p class="field-error" id="bind-error" role="alert" hidden></p>'
        )
        .replace("__GATE_ERROR__", _GATE_ERROR)
        .replace("__GATE_DOWN__", _json(outage.gate_down(_GATE_ERROR, reception_room_dial)))
    )


# Страница вместо JSON 500 на гостевых адресах (#991) — в стилях чата; подключает `app.py`.
UNAVAILABLE_PAGE: Final = outage.unavailable_page(_CSS)
