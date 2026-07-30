"""Рендеринг страниц кабинета — Jinja2 из файлов пакета (spec 0033 §4, ADR-014).

Шаблоны — `templates/` (общий каркас `layout.html`, страницы наследуют его),
статика — `static/` (мобильный CSS-канон кабинета и ванильный JS очереди).
Автоэкранирование включено — имена гостей/отелей/сотрудников попадают в HTML
безопасно.

Статика читается один раз при импорте и отдаётся из памяти (`router.py`);
версия — хэш содержимого в query-параметре ссылки из шаблона: браузер кэширует
файл надолго, а любое его изменение меняет URL. JS — отдельный файл, не
inline-`<script>`: CSP кабинета `default-src 'self'` inline-скрипты запрещает
(рекомендация ревью PR #153).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, select_autoescape

_PACKAGE_DIR: Final = Path(__file__).resolve().parent


def _static_asset(filename: str) -> tuple[str, str]:
    content = (_PACKAGE_DIR / "static" / filename).read_text(encoding="utf-8")
    return content, hashlib.sha256(content.encode()).hexdigest()[:8]


STYLES_CSS, STYLES_VERSION = _static_asset("styles.css")
QUEUE_JS, QUEUE_JS_VERSION = _static_asset("queue.js")

_environment: Final = Environment(
    loader=FileSystemLoader(_PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(("html",)),
)
_environment.globals["styles_version"] = STYLES_VERSION
_environment.globals["queue_js_version"] = QUEUE_JS_VERSION


def render_page(template_name: str, **context: Any) -> str:
    """HTML страницы кабинета по шаблону из `templates/`."""
    return _environment.get_template(template_name).render(**context)
