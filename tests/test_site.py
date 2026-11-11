"""Публичная страница necturn.com (`site/`, ADR-019): машинные обещания сайта.

Сайт — статический HTML без сборки, поэтому ни линтер, ни типы его не видят.
Тест делает проверяемыми три обещания, которые LLM-сессия, правящая сайт,
способна нарушить молча:

1. Переводы полны. Русский текст главной — сама разметка (`data-i18n` /
   `data-i18n-attr`), казахский и английский лежат в JSON-блоке `#i18n` по тем же
   ключам. Ключ без перевода страница покажет по-русски посреди казахского текста —
   ошибки не будет.
2. Ни одна страница ничего не грузит с чужих хостов и ничего не хранит в браузере
   посетителя. На этом стоит строка подвала «на сайте нет cookies, трекеров и
   аналитики»: подключённый шрифт с CDN или счётчик сделали бы её ложью.
3. В `site/` лежит только то, что сайт раздаёт намеренно (#362): Cloudflare Pages
   публикует весь каталог, и любой файл в нём — публичный адрес на necturn.com.
   Без `404.html` Pages к тому же отдаёт главную с кодом 200 на любой адрес.

Канон теста — как `tests/test_testpaths.py`: без сети и БД, только чтение файлов.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_SITE = Path(__file__).resolve().parent.parent / "site"
_SITE_INDEX = _SITE / "index.html"
# Всё, что лежит в site/, Pages раздаёт по адресу necturn.com/<путь> (кроме служебного
# `_headers`). Новый файл сюда — осознанное решение о новом публичном адресе, а заметки
# для сопровождающих живут в docs/site/README.md.
_SERVED_FILES = frozenset({"index.html", "404.html", "_headers", "presentation/index.html"})
# Мусор файловой системы Mac: в git не попадает, а значит, и в выкладку Pages.
_LOCAL_JUNK = frozenset({".DS_Store"})
_PAGES = ("index.html", "404.html", "presentation/index.html")
_TRANSLATED_LANGUAGES = ("kk", "en")
# Атрибуты, по которым браузер сам загружает ресурс. Ссылка `a href` — переход по
# клику, а `link rel=alternate` — языковая версия для поисковиков: это не загрузки.
_LOADING_ATTRIBUTES = frozenset(
    {"src", "srcset", "poster", "data", "href", "xlink:href", "action", "formaction", "background"}
)
_NAVIGATION_TAGS = frozenset({"a", "area"})
_EXTERNAL_URL_IN_CSS = re.compile(r"url\(\s*['\"]?\s*(?:https?:|//)", re.IGNORECASE)
# Обращения к сети и хранилищам браузера из скрипта страницы. Подстроки, а не
# разбор JS: скрипт маленький, а ложное срабатывание дешевле пропуска.
_FORBIDDEN_SCRIPT_APIS = (
    "document.cookie",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "fetch(",
    "XMLHttpRequest",
    "sendBeacon",
    "WebSocket",
    "EventSource",
    "import(",
)


class _PageScan(HTMLParser):
    """Один проход по разметке: ключи переводов, адреса загрузок, тексты скриптов и стилей."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.markup_keys: set[str] = set()
        self.loaded_urls: list[str] = []
        self.i18n_json = ""
        self.script_text = ""
        self.style_text = ""
        self._current: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if "data-i18n" in attributes:
            self.markup_keys.add(attributes["data-i18n"])
        for pair in attributes.get("data-i18n-attr", "").split(";"):
            _attribute, separator, key = pair.partition(":")
            if separator:
                self.markup_keys.add(key.strip())
        self._collect_loads(tag, attributes)
        # Стиль в атрибуте `style="…url(…)"` проверяется вместе с блоками <style>.
        self.style_text += attributes.get("style", "")
        if tag == "script":
            self._current = "i18n" if attributes.get("id") == "i18n" else "script"
        elif tag == "style":
            self._current = "style"

    def _collect_loads(self, tag: str, attributes: dict[str, str]) -> None:
        for name, value in attributes.items():
            if name not in _LOADING_ATTRIBUTES:
                continue
            if name == "href" and tag in _NAVIGATION_TAGS:
                continue
            if tag == "link" and attributes.get("rel") == "alternate":
                continue
            if name == "srcset":
                # «адрес ширина, адрес ширина»: проверяется каждый адрес.
                self.loaded_urls.extend(
                    candidate.split()[0] if candidate.split() else ""
                    for candidate in value.split(",")
                )
            else:
                self.loaded_urls.append(value.strip())
        if tag == "link" and "href" not in attributes:
            self.loaded_urls.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current == "i18n":
            self.i18n_json += data
        elif self._current == "script":
            self.script_text += data
        elif self._current == "style":
            self.style_text += data


def _scan_page(page: str = "index.html") -> _PageScan:
    scan = _PageScan()
    scan.feed((_SITE / page).read_text(encoding="utf-8"))
    scan.close()
    return scan


def _translations(scan: _PageScan) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = json.loads(scan.i18n_json)
    return parsed


def test_site_holds_only_files_it_serves_on_purpose() -> None:
    present = {
        path.relative_to(_SITE).as_posix()
        for path in _SITE.rglob("*")
        if path.is_file() and path.name not in _LOCAL_JUNK
    }
    assert present == _SERVED_FILES, (
        "site/ раздаётся целиком: каждый файл в нём — публичный адрес necturn.com. "
        f"Лишние: {sorted(present - _SERVED_FILES)}; нет: {sorted(_SERVED_FILES - present)}. "
        "Заметки — в docs/site/README.md; новая страница — в _SERVED_FILES осознанно."
    )


def test_every_markup_key_is_translated_and_nothing_extra() -> None:
    scan = _scan_page()
    # Защита от «пустого» прохода: сломанный разбор дал бы зелёный тест впустую.
    assert len(scan.markup_keys) > 40, "В разметке не нашлось ключей data-i18n — сломан разбор?"
    translations = _translations(scan)
    for language in _TRANSLATED_LANGUAGES:
        table = translations[language]
        missing = sorted(scan.markup_keys - table.keys())
        extra = sorted(table.keys() - scan.markup_keys)
        empty = sorted(key for key, value in table.items() if not value.strip())
        assert not missing, f"{language}: нет перевода для ключей {missing}"
        assert not extra, f"{language}: ключи без места в разметке {extra}"
        assert not empty, f"{language}: пустой перевод у ключей {empty}"


@pytest.mark.parametrize("page", _PAGES)
def test_page_loads_nothing_from_other_hosts(page: str) -> None:
    scan = _scan_page(page)
    external = [
        url for url in scan.loaded_urls if url.startswith(("http:", "https:", "//")) or not url
    ]
    assert not external, f"{page} грузит что-то извне: {external}"
    assert "@import" not in scan.style_text, f"{page}: @import в стилях"
    assert not _EXTERNAL_URL_IN_CSS.search(scan.style_text), f"{page}: url() на чужой хост"


def test_load_scan_sees_attributes_beyond_src() -> None:
    """Сторож самого разбора: загрузки мимо `src` (замечание ревью PR #352 в #362)."""
    scan = _PageScan()
    scan.feed(
        '<img srcset="/a.png 1x, https://cdn.example/b.png 2x">'
        '<video poster="//cdn.example/p.jpg"></video>'
        '<object data="https://cdn.example/o.svg"></object>'
        "<div style=\"background: url( 'https://cdn.example/bg.png')\"></div>"
        '<a href="https://wa.me/1">чат</a>'
        '<link rel="alternate" hreflang="kk" href="https://necturn.com/?lang=kk">'
    )
    scan.close()
    assert scan.loaded_urls == [
        "/a.png",
        "https://cdn.example/b.png",
        "//cdn.example/p.jpg",
        "https://cdn.example/o.svg",
    ]
    assert _EXTERNAL_URL_IN_CSS.search(scan.style_text)


@pytest.mark.parametrize("page", _PAGES)
def test_page_script_does_not_touch_network_or_browser_storage(page: str) -> None:
    scan = _scan_page(page)
    if page == "index.html":
        assert scan.script_text.strip(), "Скрипт главной не найден — сломан разбор?"
    used = [api for api in _FORBIDDEN_SCRIPT_APIS if api in scan.script_text]
    assert not used, f"{page}: скрипт обращается к сети или хранилищу браузера: {used}"
