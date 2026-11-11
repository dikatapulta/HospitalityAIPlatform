"""Выбор тестов в CI (#1057): шард полного прогона и лёгкий путь PR с документами.

Хук `pytest_collection_modifyitems` в tests/conftest.py зовёт `select`. Без переменных
окружения ниже он ничего не делает: `make check` и pytest руками гоняют всё.

`CI_TEST_SHARD=k/N` — job `tests` в `.github/workflows/ci.yml`. Полный прогон делится
на N частей по файлам: каждый файл целиком в одной части, поэтому module-фикстуры не
поднимаются дважды. Часть выбирает функция только от собранного списка — одинакового
у всех частей одного коммита, — так что любой тест попадает ровно в одну часть.
Порог покрытия считает итоговая job `check` по данным всех частей.

`CI_TEST_SCOPE=docs` — job `docs-tests`, PR только с документами
(`hospitality/tools/ci_scope.py`). Остаются файлы тестов, читающие документы: в коде —
не в докстринге и не в комментарии — есть строка с `docs` или `.md`, либо такая строка
есть во вспомогательном модуле (conftest и другие не-тестовые .py) их каталога или
каталога выше, до ближайшего `tests`. Отбор нарочно грубый: лишний файл стоит секунд,
пропущенный — красного `main`. Push в `main` гоняет всё, и промах отбора виден там же.
"""

from __future__ import annotations

import ast
import functools
import os
from collections import defaultdict
from pathlib import Path

import pytest

SHARD_ENV = "CI_TEST_SHARD"
SCOPE_ENV = "CI_TEST_SCOPE"
DOCS_SCOPE = "docs"

_DOC_MARKERS = ("docs", ".md")
_TESTS_DIR = "tests"
_SELF = Path(__file__).resolve()
_APPLIED = pytest.StashKey[bool]()


def parse_shard(value: str) -> tuple[int, int]:
    """`k/N` → (k, N), 1 ≤ k ≤ N; иначе — ошибка запуска, а не тихий прогон всего."""
    try:
        index_text, total_text = value.split("/")
        index, total = int(index_text), int(total_text)
    except ValueError:
        raise pytest.UsageError(f"{SHARD_ENV}={value!r}: ожидается k/N, например 1/3") from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"{SHARD_ENV}={value!r}: k от 1 до N")
    return index, total


def assign_shards(weights: dict[str, int], total: int) -> dict[str, int]:
    """Файл → номер части 1…total: самый тяжёлый файл — в самую лёгкую часть (LPT).

    Порядок обхода задан весом и путём, поэтому результат зависит только от списка.
    """
    loads = [0] * total
    shards: dict[str, int] = {}
    for path in sorted(weights, key=lambda item: (-weights[item], item)):
        lightest = min(range(total), key=lambda shard: (loads[shard], shard))
        shards[path] = lightest + 1
        loads[lightest] += weights[path]
    return shards


def _docstring_ids(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, owners) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


@functools.cache
def mentions_docs(path: Path) -> bool:
    """В коде файла есть строка с `docs` или `.md` (докстринги не в счёт)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_ids(tree)
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and any(marker in node.value for marker in _DOC_MARKERS)
        for node in ast.walk(tree)
    )


@functools.cache
def _helpers_mention_docs(directory: Path) -> bool:
    """Вспомогательный модуль каталога (conftest и другие не-тестовые .py) читает документы."""
    return any(
        mentions_docs(path)
        for path in sorted(directory.glob("*.py"))
        if not path.name.startswith("test_") and path.resolve() != _SELF
    )


def _helper_dirs(test_dir: Path) -> list[Path]:
    """Каталог файла тестов и каталоги выше — до ближайшего `tests` включительно."""
    found: list[Path] = []
    for directory in (test_dir, *test_dir.parents):
        found.append(directory)
        if directory.name == _TESTS_DIR:
            return found
    return [test_dir]


def reads_docs(test_file: Path) -> bool:
    """Файл тестов попадает в лёгкий путь PR с документами."""
    return mentions_docs(test_file) or any(
        _helpers_mention_docs(directory) for directory in _helper_dirs(test_file.parent)
    )


def _test_file(item: pytest.Item) -> str:
    """Файл теста от корня репозитория — одинаковый на любом раннере."""
    return item.nodeid.split("::", 1)[0]


def select(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Оставить в `items` тесты своей части и своего пути; остальное — deselected.

    Второй вызов (хук попал бы в ещё один conftest) — ничего: деление уже
    отобранного на N частей потеряло бы тесты.
    """
    if config.stash.get(_APPLIED, False):
        return
    config.stash[_APPLIED] = True
    scope = os.environ.get(SCOPE_ENV, "")
    shard = os.environ.get(SHARD_ENV, "")
    if scope not in ("", DOCS_SCOPE):
        raise pytest.UsageError(f"{SCOPE_ENV}={scope!r}: допустимо только {DOCS_SCOPE!r}")
    keep = items
    if scope == DOCS_SCOPE:
        keep = [item for item in keep if reads_docs(item.path)]
    if shard:
        index, total = parse_shard(shard)
        weights: dict[str, int] = defaultdict(int)
        for item in keep:
            weights[_test_file(item)] += 1
        shards = assign_shards(weights, total)
        keep = [item for item in keep if shards[_test_file(item)] == index]
    if len(keep) == len(items):
        return
    kept = set(map(id, keep))
    config.hook.pytest_deselected(items=[item for item in items if id(item) not in kept])
    items[:] = keep
