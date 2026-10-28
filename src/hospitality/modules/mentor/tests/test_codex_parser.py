"""Разбор кодекса (spec 0048 §3) — образцы из тестов «Наставника» (`tests/test_kodeks.py`)."""

from __future__ import annotations

import re

import pytest

from hospitality.modules.mentor.codex import MAX_ARTICLE_CHARS, CodexMarkupError, parse_codex
from hospitality.modules.mentor.tests.conftest import BODY, HEAD

OK = """# Общая часть
## Раздел 1. Общие положения
### Глава 1. О кодексе
> Основание: проба.
#### Статья 1. Первая
1. Пункт:
1) подпункт;
2) ещё подпункт.
2. Второй пункт.
Источник: проба.
# Особенная часть
## Раздел 2. Служба
### Глава 2. Консьерж
#### Статья 2. Вторая
Абзац без номера.
Источник: проба.
#### Статья 2-1. Вставка
1. Текст.
Источник: проба.
#### Статья 3. Третья
1. Текст.
Источник: проба.
"""


def _parse(body: str, head: str = "Название: Кодекс\n") -> object:
    return parse_codex([("10.md", body), ("00.md", head)])


def test_structure_points_and_inserted_article() -> None:
    codex = parse_codex([("10.md", OK), ("00.md", "Название: Кодекс\n")])
    assert codex.meta == {"Название": "Кодекс"}
    assert [p.title for p in codex.parts] == ["Общая часть", "Особенная часть"]
    first = codex.articles[0]
    assert first.anchor == "st-1" and first.source == "проба."
    assert [(p.number, len(p.subpoints)) for p in first.points] == [("1", 2), ("2", 0)]
    assert first.points[0].subpoints[1] == ("2", "ещё подпункт.")
    assert codex.parts[0].sections[0].chapters[0].note == "Основание: проба."
    assert codex.articles[1].points[0].number == ""  # абзац без номера
    assert [a.number for a in codex.articles] == ["1", "2", "2-1", "3"]
    assert [c.title for c in codex.positions] == ["Консьерж"]


def test_body_keeps_numbers_and_drops_source() -> None:
    first = parse_codex([("10.md", OK), ("00.md", "Название: Кодекс\n")]).articles[0]
    assert first.body == "1. Пункт:\n1) подпункт;\n2) ещё подпункт.\n2. Второй пункт."


def test_files_are_read_in_name_order() -> None:
    # Шапка в «00», тело в «10»: переданы в обратном порядке — разбор тот же.
    codex = parse_codex([("10_body.md", BODY), ("00_head.md", HEAD)])
    assert codex.meta["Название"] == "Кодекс отеля «Тест»"
    assert [chapter.number for _, chapter, _ in codex.walk()] == [1, 2, 2, 3]


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        (OK.replace("Источник: проба.\n# Особенная", "# Особенная"), "нет строки «Источник"),
        (OK.replace("#### Статья 3.", "#### Статья 4."), "статья 4 вместо 3"),
        (OK.replace("#### Статья 2-1.", "#### Статья 2-2."), "вставка"),
        (OK.replace("2. Второй пункт.", "3. Второй пункт."), "пункт 3. вместо 2."),
        (OK.replace("2) ещё подпункт.", "3) ещё подпункт."), "подпункт 3) вместо 2)"),
        (OK.replace("### Глава 2.", "### Глава 3."), "глава 3 вместо 2"),
        (OK.replace("## Раздел 2.", "## Раздел 3."), "раздел 3 вместо 2"),
        (OK.replace("1. Пункт:\n", ""), "подпункт «1)» без пункта"),
        (
            OK.replace("Источник: проба.\n# Особенная", "Источник: проба.\nХвост.\n# Особенная"),
            "последняя строка",
        ),
        ("Текст до заголовков\n" + OK, "Ключ: значение"),
        (
            OK.replace("#### Статья 1. Первая", "Свободный текст\n#### Статья 1. Первая"),
            "текст вне статьи",
        ),
    ],
)
def test_markup_errors_name_the_file_and_line(broken: str, message: str) -> None:
    with pytest.raises(CodexMarkupError, match=re.escape(message)) as error:
        _parse(broken)
    assert re.match(r"Файл (10|00)\.md, строка \d+: ", str(error.value))


def test_error_names_the_exact_line() -> None:
    # Строка 20 тела — «#### Статья 4. Третья» вместо 3.
    with pytest.raises(CodexMarkupError) as error:
        _parse(OK.replace("#### Статья 3.", "#### Статья 4."))
    assert str(error.value) == "Файл 10.md, строка 20: статья 4 вместо 3"
    assert (error.value.file_name, error.value.line) == ("10.md", 20)


def test_missing_title_is_an_error() -> None:
    with pytest.raises(CodexMarkupError, match="Название"):
        _parse(OK, head="Статус: Проект\n")


def test_too_long_article_points_at_its_heading() -> None:
    long_text = "Слово " * (MAX_ARTICLE_CHARS // 6 + 1)
    with pytest.raises(CodexMarkupError, match="статья 3 длиннее") as error:
        _parse(
            OK.replace("#### Статья 3. Третья\n1. Текст.", f"#### Статья 3. Третья\n{long_text}")
        )
    assert error.value.line == 20


def test_duplicate_file_names_and_no_files_are_errors() -> None:
    with pytest.raises(CodexMarkupError, match="два файла с одним именем"):
        parse_codex([("10.md", OK), ("10.md", OK)])
    with pytest.raises(CodexMarkupError, match="нет ни одного файла"):
        parse_codex([])
