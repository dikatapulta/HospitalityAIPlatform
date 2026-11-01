"""Рендеринг страниц кабинета — Jinja2 из файлов пакета (spec 0033 §4, ADR-014).

Шаблоны — `templates/` (общий каркас `layout.html`, страницы наследуют его),
статика — `static/` (мобильный CSS-канон кабинета и ванильный JS страниц).
Автоэкранирование включено — имена гостей/отелей/сотрудников попадают в HTML
безопасно.

Статика читается один раз при импорте и отдаётся из памяти (`router.py`);
версия — хэш содержимого в query-параметре ссылки из шаблона: браузер кэширует
файл надолго, а любое его изменение меняет URL. JS — отдельный файл, не
inline-`<script>`: CSP кабинета `default-src 'self'` inline-скрипты запрещает
(рекомендация ревью PR #153).

Новый файл статики — одна строка в `_STATIC_FILES`: отдачу и кэш-бастинг он
получает автоматически, отдельного маршрута заводить не нужно (PR F).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, select_autoescape

_PACKAGE_DIR: Final = Path(__file__).resolve().parent

_CSS: Final = "text/css; charset=utf-8"
_JS: Final = "text/javascript; charset=utf-8"
# Шрифт кабинета Onest (issue #462, лицензия OFL — static/onest-OFL.txt): свой
# файл, а не Google Fonts — CSP `default-src 'self'` чужие шрифты не пускает.
_FONT: Final = "font/woff2"
# Иконки и манифест веб-приложения на экране «Домой» (spec 0050 §3).
_PNG: Final = "image/png"
_MANIFEST: Final = "application/manifest+json"
_BINARY_TYPES: Final = frozenset({_FONT, _PNG})

# Единственный список статики кабинета: файл, MIME-тип, имя переменной шаблона
# с версией для кэш-бастинга (`href=…?v={{ styles_version }}`).
_STATIC_FILES: Final[tuple[tuple[str, str, str], ...]] = (
    ("styles.css", _CSS, "styles_version"),
    ("queue.js", _JS, "queue_js_version"),
    ("turnovers.js", _JS, "turnovers_js_version"),
    ("floors.js", _JS, "floors_js_version"),
    ("act_requests.js", _JS, "act_requests_js_version"),
    ("act_requests.css", _CSS, "act_requests_css_version"),
    # Панель «Товары и меню» (spec 0038 §6 PR 4): своя раскладка и скрипт.
    ("catalog.css", _CSS, "catalog_css_version"),
    ("catalog.js", _JS, "catalog_js_version"),
    # Поле «Фото» формы товара (PR 7): выбор, предпросмотр, загрузка.
    ("catalog_photo.js", _JS, "catalog_photo_js_version"),
    # «Наставник» (spec 0048 §10 PR 4): чат с потоком, кодекс и статистика.
    ("mentor.css", _CSS, "mentor_css_version"),
    ("mentor_chat.js", _JS, "mentor_chat_js_version"),
    ("mentor_codex.js", _JS, "mentor_codex_js_version"),
    ("checkin.js", _JS, "checkin_js_version"),
    ("team.js", _JS, "team_js_version"),
    ("new_request.js", _JS, "new_request_js_version"),
    ("knowledge.js", _JS, "knowledge_js_version"),
    ("demo_handover.css", _CSS, "demo_handover_css_version"),
    ("demo_handover.js", _JS, "demo_handover_js_version"),
    ("demo_handover_board.css", _CSS, "demo_handover_board_css_version"),
    ("demo_handover_board.js", _JS, "demo_handover_board_js_version"),
    ("demo_room_board.css", _CSS, "demo_room_board_css_version"),
    ("demo_room_board.js", _JS, "demo_room_board_js_version"),
    # Демо «Забытые вещи» (#604): поиск, форма и выдача — только в браузере.
    ("demo_lost_found.css", _CSS, "demo_lost_found_css_version"),
    ("demo_lost_found.js", _JS, "demo_lost_found_js_version"),
    ("signal.js", _JS, "signal_js_version"),
    # Веб-пуш (spec 0050): кнопка и подписка телефона; service worker отдаёт
    # свой маршрут `/staff/sw.js` — его область обязана покрывать весь `/staff/`.
    ("push.js", _JS, "push_js_version"),
    ("sw.js", _JS, "sw_js_version"),
    # Манифест ссылается на иконки без версии в URL, как шрифты в shell.css:
    # новая картинка — новое имя файла.
    ("manifest.webmanifest", _MANIFEST, "manifest_version"),
    ("icon-192.png", _PNG, "icon_192_version"),
    ("icon-512.png", _PNG, "icon_512_version"),
    ("apple-touch-icon.png", _PNG, "apple_touch_icon_version"),
    ("badge-96.png", _PNG, "badge_96_version"),
    ("home.css", _CSS, "home_css_version"),
    ("shell.css", _CSS, "shell_css_version"),
    # Шрифт подключает @font-face в shell.css без версии в URL: файл кэшируется
    # навсегда, поэтому новая версия шрифта — новое имя файла.
    ("onest-latin.woff2", _FONT, "onest_latin_version"),
    ("onest-cyrillic.woff2", _FONT, "onest_cyrillic_version"),
    ("onest-cyrillic-ext.woff2", _FONT, "onest_cyrillic_ext_version"),
)


@dataclass(frozen=True)
class StaticAsset:
    """Файл статики в памяти: содержимое, MIME-тип и версия (хэш содержимого).

    Текст (CSS, JS, манифест) — строкой, шрифт и картинки — байтами."""

    content: str | bytes
    media_type: str
    version: str


def _load(filename: str, media_type: str) -> StaticAsset:
    raw = (_PACKAGE_DIR / "static" / filename).read_bytes()
    content: str | bytes = raw if media_type in _BINARY_TYPES else raw.decode("utf-8")
    return StaticAsset(
        content=content,
        media_type=media_type,
        version=hashlib.sha256(raw).hexdigest()[:8],
    )


STATIC_ASSETS: Final[dict[str, StaticAsset]] = {
    filename: _load(filename, media_type) for filename, media_type, _ in _STATIC_FILES
}

_environment: Final = Environment(
    loader=FileSystemLoader(_PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(("html",)),
)
for _filename, _, _version_variable in _STATIC_FILES:
    _environment.globals[_version_variable] = STATIC_ASSETS[_filename].version


def render_page(template_name: str, **context: Any) -> str:
    """HTML страницы кабинета по шаблону из `templates/`."""
    return _environment.get_template(template_name).render(**context)
