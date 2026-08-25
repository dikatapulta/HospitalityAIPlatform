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

# Единственный список статики кабинета: файл, MIME-тип, имя переменной шаблона
# с версией для кэш-бастинга (`href=…?v={{ styles_version }}`).
_STATIC_FILES: Final[tuple[tuple[str, str, str], ...]] = (
    ("styles.css", _CSS, "styles_version"),
    ("queue.js", _JS, "queue_js_version"),
    ("checkin.js", _JS, "checkin_js_version"),
    ("team.js", _JS, "team_js_version"),
    ("new_request.js", _JS, "new_request_js_version"),
)


@dataclass(frozen=True)
class StaticAsset:
    """Файл статики в памяти: содержимое, MIME-тип и версия (хэш содержимого)."""

    content: str
    media_type: str
    version: str


def _load(filename: str, media_type: str) -> StaticAsset:
    content = (_PACKAGE_DIR / "static" / filename).read_text(encoding="utf-8")
    return StaticAsset(
        content=content,
        media_type=media_type,
        version=hashlib.sha256(content.encode()).hexdigest()[:8],
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
