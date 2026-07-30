"""Рендеринг страниц кабинета — Jinja2 из файлов пакета (spec 0033 §4, ADR-014).

Шаблоны — `templates/` (общий каркас `layout.html`, страницы наследуют его),
стили — `static/styles.css` (мобильный CSS-канон кабинета). Автоэкранирование
включено — имена гостей/отелей/сотрудников попадают в HTML безопасно.

CSS читается один раз при импорте и отдаётся из памяти (`router.py`);
`STYLES_VERSION` — хэш содержимого в query-параметре ссылки из layout:
браузер кэширует стили надолго, а любое их изменение меняет URL.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, select_autoescape

_PACKAGE_DIR: Final = Path(__file__).resolve().parent

STYLES_CSS: Final = (_PACKAGE_DIR / "static" / "styles.css").read_text(encoding="utf-8")
STYLES_VERSION: Final = hashlib.sha256(STYLES_CSS.encode()).hexdigest()[:8]

_environment: Final = Environment(
    loader=FileSystemLoader(_PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(("html",)),
)
_environment.globals["styles_version"] = STYLES_VERSION


def render_page(template_name: str, **context: Any) -> str:
    """HTML страницы кабинета по шаблону из `templates/`."""
    return _environment.get_template(template_name).render(**context)
