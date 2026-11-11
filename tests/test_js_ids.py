"""Каждый id, который JavaScript ищет литералом, объявлен в разметке (#1056 п. 3).

JS кабинета и гостевых страниц находит элементы по id: `getElementById("x")`,
`querySelector("#x …")` и обёртка `$` — по id в `chat.js` и `mentor_codex.js`, селектором
в `menu.js`, `documents.js`, `table.js`. Переименовал id в шаблоне и забыл JS — поиск
вернёт null, первый же `addEventListener` бросит исключение, и экран мёртв (экран
экзамена «Наставника» целиком на JS). Тесты страниц и линтеры этого не видят.

Тест собирает такие id из `.js` в `src/` и ищет объявление каждого: `id="x"` в шаблонах
`.html`, в Python, строящем HTML, и в JS, создающем элементы (`.id = "x"`,
`setAttribute("id", "x")`). Сверка — по всему `src/`, а не по странице: id, живой
на другой странице, тест не отличит, зато правило обходится без карты «скрипт → страница».
Поиск собранной строкой (`"#sec-" + key`) и по переменной не проверяется.

Id, объявленный косвенно (аргумент макроса или функции, склейка в JS), — в `_INDIRECT`
вместе со строками, которые его объявляют: пропала строка — тест красный, как и при
пропавшем id.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

_LITERAL = r"""(?P<q>["'`])(?P<arg>[^"'`\n]*)(?P=q)\s*(?P<after>[,)+])"""
_LOOKUP = re.compile(
    r"(?:(?P<call>getElementById|querySelector(?:All)?)|(?<![\w$])(?P<dollar>\$))\(\s*" + _LITERAL
)
# `$ = (id) => document.getElementById(id)` и `$ = function (id) { return …getElementById(id) }`.
_DOLLAR_BY_ID = re.compile(
    r"\$\s*=\s*(?:function\s*)?\(\s*(\w+)\s*\)\s*(?:=>|\{)[^;]*?getElementById\(\s*\1\s*\)"
)
_SELECTOR_ID = re.compile(r"#(?P<id>[A-Za-z][\w-]*)")
_ID_ATTR = re.compile(r"""(?<![\w.-])id\s*=\s*\\?["'](?P<id>[\w-]+)\\?["']""")
_ID_IN_JS = re.compile(
    r"""(?:\.id\s*=\s*|setAttribute\(\s*["']id["']\s*,\s*)["'`](?P<id>[\w-]+)["'`]"""
)
_DECLARING_SUFFIXES = (".html", ".py", ".js")


class _Indirect(NamedTuple):
    path: str
    lines: tuple[str, ...]


_INDIRECT: dict[str, _Indirect] = {
    # Макрос `topics(id, …)` рисует `<select id="{{ id }}">`.
    "quiz-topic": _Indirect(
        "src/hospitality/staff_portal/templates/mentor_chat.html",
        ('topics("quiz-topic"', 'id="{{ id }}"'),
    ),
    "exam-topic": _Indirect(
        "src/hospitality/staff_portal/templates/mentor_chat.html",
        ('topics("exam-topic"', 'id="{{ id }}"'),
    ),
    # Плашка над строкой ввода: `_action(link_id, …)` рисует `id="{link_id}"`.
    "orders-link": _Indirect(
        "src/hospitality/channels/web/page.py",
        ('_action("orders-link"', 'id="{link_id}"'),
    ),
    # Поля формы витрины: `field(name, …)` ставит `input.id = "sr-" + name`.
    "sr-company": _Indirect(
        "src/hospitality/channels/showroom/assets/request_form.js",
        ('text("company"', 'input.id = "sr-" + name'),
    ),
    "sr-contact_name": _Indirect(
        "src/hospitality/channels/showroom/assets/request_form.js",
        ('text("contact_name"', 'input.id = "sr-" + name'),
    ),
}


def lookups_in(text: str) -> set[str]:
    """Id, которые JS ищет литералом: целая строка, без склейки и подстановки `${…}`."""
    dollar_by_id = bool(_DOLLAR_BY_ID.search(text))
    found = set()
    for match in _LOOKUP.finditer(text):
        arg = match["arg"]
        if match["after"] == "+" or "${" in arg:
            continue
        by_id = match["dollar"] and dollar_by_id and not arg.startswith("#")
        if match["call"] == "getElementById" or by_id:
            found.add(arg)
        elif selector := _SELECTOR_ID.match(arg):
            found.add(selector["id"])
    return found


def declarations_in(path: Path, text: str) -> set[str]:
    found = {match["id"] for match in _ID_ATTR.finditer(text)}
    if path.suffix == ".js":
        found |= {match["id"] for match in _ID_IN_JS.finditer(text)}
    return found


def _sources(suffixes: Iterable[str]) -> dict[Path, str]:
    wanted = tuple(suffixes)
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*"))
        if path.suffix in wanted
        and "tests" not in path.relative_to(SRC).parts
        and not path.name.startswith("test_")
        and "__pycache__" not in path.parts
        and ".min." not in path.name
    }


def _scan() -> tuple[dict[str, set[str]], set[str]]:
    looked_up: dict[str, set[str]] = {}
    declared: set[str] = set()
    for path, text in _sources(_DECLARING_SUFFIXES).items():
        declared |= declarations_in(path, text)
        if path.suffix == ".js":
            for element_id in lookups_in(text):
                looked_up.setdefault(element_id, set()).add(str(path.relative_to(ROOT)))
    return looked_up, declared


def test_lookup_forms() -> None:
    script = """
        document.getElementById("by-id");
        root.querySelector("#by-selector .item");
        document.querySelectorAll('#all > li');
        document.getElementById("glued-" + key);
        document.querySelector(`#${dynamic}`);
        document.querySelector(".class-only");
        document.getElementById(variable);
    """
    assert lookups_in(script) == {"by-id", "by-selector", "all"}
    by_id = 'const $ = (id) => document.getElementById(id);\n$("plain"); $("#also");'
    assert lookups_in(by_id) == {"plain", "also"}
    by_selector = 'const $ = (s, r) => (r || document).querySelector(s);\n$("#sheet"); $(".x");'
    assert lookups_in(by_selector) == {"sheet"}


def test_declaration_forms() -> None:
    markup = """<a id="double"></a><b id='single'></b> f'<i id=\\"escaped\\">' <p data-id="not">"""
    assert declarations_in(Path("x.html"), markup) == {"double", "single", "escaped"}
    script = 'node.id = "assigned"; node.setAttribute("id", "attribute"); x.id = name;'
    assert declarations_in(Path("x.js"), script) == {"assigned", "attribute"}
    assert declarations_in(Path("x.py"), script) == set()


def test_scan_sees_every_lookup_form() -> None:
    """Сломанный разбор дал бы пустой список — и зелёную сверку ниже."""
    looked_up, _ = _scan()
    assert "src/hospitality/staff_portal/static/mentor_exam.js" in looked_up["exam-start"]
    assert "src/hospitality/channels/web/assets/chat.js" in looked_up["composer"]
    assert "src/hospitality/channels/web/menu/assets/menu.js" in looked_up["sheet"]
    assert len(looked_up) >= 100


def test_every_js_id_lookup_is_declared() -> None:
    looked_up, declared = _scan()
    missing = {
        element_id: sorted(files)
        for element_id, files in looked_up.items()
        if element_id not in declared and element_id not in _INDIRECT
    }
    assert not missing, (
        "JS ищет id, которого нет ни в шаблонах, ни в Python, ни в JS, создающем элементы "
        f"(переименован или удалён?): {missing}. Объявлен косвенно — строка в _INDIRECT."
    )


def test_indirect_ids_are_still_indirect() -> None:
    looked_up, declared = _scan()
    for element_id, (path, lines) in _INDIRECT.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        gone = [line for line in lines if line not in text]
        assert not gone, f"{element_id}: в {path} нет строк, которые его объявляют: {gone}"
        assert element_id in looked_up, f"{element_id}: JS его больше не ищет — убрать из _INDIRECT"
        assert element_id not in declared, f"{element_id}: объявлен прямо — убрать из _INDIRECT"
