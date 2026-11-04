"""Страница витрины «как прототип» (#816, часть Б — #819): шапка, надписи, статика.

Без базы: `render_page` — чистая функция. Проверяем то, что ломает страницу молча:
модуль, импортированный скриптом, но не отданный маршрутом статики (404 в браузере —
пустая страница); надпись, которой нет на одном из языков; название отеля, не
экранированное в шапке; предел группы, разошедшийся с пределом запроса; атрибут `style`
из скрипта или в разметке — CSP страницы и кабинета его не применяет (#824).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hospitality.channels.showroom import assets, page
from hospitality.channels.showroom.texts import TEXTS
from hospitality.channels.showroom.texts_ui import UI_TEXTS
from hospitality.modules.showroom import api as showroom_api

_CABINET_TEMPLATE = Path(__file__).parents[3] / "staff_portal" / "templates" / "showroom.html"
_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*["']\./([\w.-]+)["']""")
# Атрибут `style`: ключ в `h(..., {style: …})`, `setAttribute("style")`, `style="…"` в разметке.
# Свойства `element.style.…` CSP пропускает — их выражение не ловит.
_INLINE_STYLE = re.compile(
    r"""(?<![\w.-])["']?style["']?\s*:"""
    r"""|setAttribute\(\s*["']style"""
    r"""|(?<![\w.-])style=\\?["']"""
)


def test_every_relative_import_is_a_served_asset() -> None:
    js = [name for name in assets.ASSETS if name.endswith(".js") and not name.startswith("three-")]
    imported = {
        target
        for name in js
        for target in _IMPORT.findall(assets.ASSETS[name].content.decode("utf-8"))
    }
    assert {"showroom_core.js", "showroom_stage.js", "showroom_panel.js"} <= imported
    assert imported <= set(assets.ASSETS), imported - set(assets.ASSETS)


def test_ui_texts_have_same_keys_and_plural_forms_in_every_language() -> None:
    keys = {language: set(texts) for language, texts in UI_TEXTS.items()}
    assert keys["ru"] == keys["en"] == keys["kk"]
    forms = {"ru": 3, "en": 2, "kk": 1}
    for language, count in forms.items():
        for key in ("stat_floors", "stat_rooms", "stat_sale"):
            assert len(UI_TEXTS[language][key].split("|")) == count, (language, key)
    for language, texts in TEXTS.items():
        # Новые надписи дошли до страницы как есть.
        assert UI_TEXTS[language].items() <= texts.items()


def test_header_shows_escaped_hotel_name_stats_slot_and_group_limit() -> None:
    html = page.render_page(slug="demo", hotel_name="Hotel <b>&", language="kk")
    assert "<h1>Hotel &lt;b&gt;&amp;</h1>" in html
    assert 'class="sr-stats" data-sr-stats' in html
    assert UI_TEXTS["kk"]["eyebrow"] in html
    assert 'aria-current="true">ҚАЗ</a>' in html
    config = json.loads(re.search(r'id="sr-config">(.*?)</script>', html).group(1))  # type: ignore[union-attr]
    assert config["maxGroupRooms"] == showroom_api.MAX_GROUP_ROOMS
    assert config["texts"]["tab_floors"] == "Қабаттар"


def test_no_inline_style_attribute_in_scripts_or_page() -> None:
    sources = {
        name: asset.content.decode("utf-8")
        for name, asset in assets.ASSETS.items()
        if name.endswith(".js") and not name.startswith("three-")
    }
    sources["page.html"] = page.render_page(slug="demo", hotel_name="Hotel", language="ru")
    sources["showroom.html"] = _CABINET_TEMPLATE.read_text(encoding="utf-8")
    found = [
        f"{name}:{number}"
        for name, text in sources.items()
        for number, line in enumerate(text.splitlines(), 1)
        if _INLINE_STYLE.search(line)
    ]
    assert not found, found
