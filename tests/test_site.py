"""Публичная страница necturn.com (`site/index.html`, ADR-019): машинные обещания страницы.

Сайт — один статический HTML без сборки, поэтому ни линтер, ни типы его не видят.
Тест делает проверяемыми два обещания, которые LLM-сессия, правящая тексты,
способна нарушить молча:

1. Переводы полны. Русский текст — сама разметка (`data-i18n` / `data-i18n-attr`),
   казахский и английский лежат в JSON-блоке `#i18n` по тем же ключам. Ключ без
   перевода страница покажет по-русски посреди казахского текста — ошибки не будет.
2. Страница ничего не грузит с чужих хостов и ничего не хранит в браузере
   посетителя. На этом стоит строка подвала «на сайте нет cookies, трекеров и
   аналитики»: подключённый шрифт с CDN или счётчик сделали бы её ложью.

Канон теста — как `tests/test_testpaths.py`: без сети и БД, только чтение файла.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

_SITE_INDEX = Path(__file__).resolve().parent.parent / "site" / "index.html"
_TRANSLATED_LANGUAGES = ("kk", "en")
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
    """Один проход по разметке: ключи переводов, адреса загрузок, тексты скриптов."""

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
        if "src" in attributes:
            self.loaded_urls.append(attributes["src"])
        # rel=alternate — ссылка на языковую версию для поисковиков, не загрузка.
        if tag == "link" and attributes.get("rel") != "alternate":
            self.loaded_urls.append(attributes.get("href", ""))
        if tag == "script":
            self._current = "i18n" if attributes.get("id") == "i18n" else "script"
        elif tag == "style":
            self._current = "style"

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


def _scan_page() -> _PageScan:
    scan = _PageScan()
    scan.feed(_SITE_INDEX.read_text(encoding="utf-8"))
    scan.close()
    return scan


def _translations(scan: _PageScan) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = json.loads(scan.i18n_json)
    return parsed


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


def test_page_loads_nothing_from_other_hosts() -> None:
    scan = _scan_page()
    external = [
        url for url in scan.loaded_urls if url.startswith(("http:", "https:", "//")) or not url
    ]
    assert not external, f"Страница грузит что-то извне: {external}"
    assert "@import" not in scan.style_text
    assert "url(http" not in scan.style_text and "url(//" not in scan.style_text


def test_page_script_does_not_touch_network_or_browser_storage() -> None:
    scan = _scan_page()
    assert scan.script_text.strip(), "Скрипт страницы не найден — сломан разбор?"
    used = [api for api in _FORBIDDEN_SCRIPT_APIS if api in scan.script_text]
    assert not used, f"Скрипт страницы обращается к сети или хранилищу браузера: {used}"
