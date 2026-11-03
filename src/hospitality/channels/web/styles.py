"""Общие токены гостевых страниц (#746): один набор цветов, шрифтов и радиусов.

Чат, вход по листку, «Заказать в номер», «Забронировать столик» и форма реквизитов
вклеивают `TOKENS_CSS` первым в свои стили, а не держат копию палитры каждая: до
#746 у трёх гостевых экранов было три палитры. Свои стили страницы пользуются
только переменными отсюда.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

TOKENS_CSS: Final = (Path(__file__).resolve().parent / "assets" / "tokens.css").read_text(
    encoding="utf-8"
)

# Цвет строки состояния браузера — фон страниц (`--bg` в `tokens.css`).
THEME_COLOR_META: Final = (
    '<meta name="theme-color" content="#f2f6fa" media="(prefers-color-scheme: light)">\n'
    '<meta name="theme-color" content="#0c1625" media="(prefers-color-scheme: dark)">'
)


def compact_css(css: str) -> str:
    """Стили без комментариев и лишних пробелов — вес страницы, а не смысл.

    Условие #746: страница чата не тяжелее прежней вдвое, а гость открывает её на
    гостиничном Wi-Fi. Пробел ПЕРЕД двоеточием не трогается: `.a :hover` и
    `.a:hover` — разные селекторы.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};])\s*", r"\1", css).replace(";}", "}")
    return re.sub(r"([:,]) ", r"\1", css).strip()


def compact_js(js: str) -> str:
    """Скрипт без комментариев и отступов; переводы строк остаются (ASI).

    Убираются строки `// …` и вставки `/* … */`; комментарий в конце строки кода
    остаётся — его пишут отдельной строкой. Годится для скриптов без многострочных
    строк и без `/*` внутри строк — у гостевых страниц их нет.
    """
    js = re.sub(r"\s*/\*.*?\*/", "", js)
    lines = (line.strip() for line in js.split("\n"))
    return "\n".join(line for line in lines if line and not line.startswith("//"))
