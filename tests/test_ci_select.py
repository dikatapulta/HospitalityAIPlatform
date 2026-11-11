"""Выбор тестов в CI (#1057, tests/ci_select.py): части полного прогона и лёгкий путь.

Главное обещание частей — каждый тест ровно в одной из них: потерянный тест не виден
ни в одном логе. Лёгкого пути — тест, читающий документы, в него попадает.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests import ci_select
from tests.ci_select import assign_shards, mentions_docs, parse_shard, reads_docs

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ci_select.SHARD_ENV, raising=False)
    monkeypatch.delenv(ci_select.SCOPE_ENV, raising=False)


def test_parse_shard() -> None:
    assert parse_shard("1/3") == (1, 3)
    assert parse_shard("3/3") == (3, 3)
    for wrong in ("0/3", "4/3", "1/0", "1-3", "x/3", "", "1/3/5"):
        with pytest.raises(pytest.UsageError):
            parse_shard(wrong)


def test_every_file_lands_in_exactly_one_shard() -> None:
    weights = {f"tests/test_{n:03}.py": (n * 37) % 11 + 1 for n in range(120)}
    shards = assign_shards(weights, 3)
    assert set(shards) == set(weights)
    assert set(shards.values()) == {1, 2, 3}
    loads = [sum(w for path, w in weights.items() if shards[path] == k) for k in (1, 2, 3)]
    # LPT: самая тяжёлая часть не тяжелее лёгкой больше чем на самый тяжёлый файл.
    assert max(loads) - min(loads) <= max(weights.values())


def test_shards_do_not_depend_on_collection_order() -> None:
    weights = {"b.py": 3, "a.py": 3, "c.py": 1, "d.py": 5}
    reordered = dict(reversed(list(weights.items())))
    assert assign_shards(weights, 2) == assign_shards(reordered, 2)


def test_more_shards_than_files() -> None:
    assert assign_shards({"a.py": 1}, 3) == {"a.py": 1}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_doc_literal_in_code_counts_docstring_and_comment_do_not(tmp_path: Path) -> None:
    quiet = _write(
        tmp_path / "test_quiet.py",
        '"""Модуль: правило — docs/runbooks/errors.md."""\n'
        "# см. docs/QUEUE.md\n"
        "def test_x() -> None:\n"
        '    """Сверка с README.md."""\n'
        "    assert 1\n",
    )
    path_literal = _write(
        tmp_path / "test_path.py",
        'ROOT = None\nQUEUE = ("docs", "QUEUE.md")\n',
    )
    f_string = _write(
        tmp_path / "test_fstring.py", 'def name(x: str) -> str:\n    return f"{x}.md"\n'
    )
    assert not mentions_docs(quiet)
    assert mentions_docs(path_literal)
    assert mentions_docs(f_string)


def test_helper_of_own_tests_tree_counts(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "conftest.py", 'DOCS = "docs"\n')
    inner = _write(tmp_path / "tests" / "smoke" / "test_inner.py", "def test_x() -> None: ...\n")
    _write(tmp_path / "pkg" / "helper.py", 'DOCS = "docs"\n')
    outside = _write(tmp_path / "pkg" / "tests" / "test_out.py", "def test_x() -> None: ...\n")
    assert reads_docs(inner)
    # helper.py пакета — не вспомогательный модуль тестов: выше ближайшего tests не ищем.
    assert not reads_docs(outside)


def test_real_suite_selection() -> None:
    # Канарейка на настоящих файлах: сверка каталога ошибок с рунбуком — в лёгком пути,
    # тест изоляции тенантов документов не читает.
    assert reads_docs(_REPO_ROOT / "tests" / "test_errors.py")
    assert reads_docs(_REPO_ROOT / "tests" / "test_queue_script.py")
    assert not reads_docs(_REPO_ROOT / "tests" / "test_tenant_isolation.py")


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[object] = []

    def pytest_deselected(self, items: list[object]) -> None:
        self.deselected.extend(items)


def _config() -> tuple[pytest.Config, _Hook]:
    hook = _Hook()
    return cast(pytest.Config, SimpleNamespace(stash=pytest.Stash(), hook=hook)), hook


def _items(paths: Mapping[str, Path | None]) -> list[pytest.Item]:
    found: list[pytest.Item] = []
    for nodeid, path in paths.items():
        found.append(cast(pytest.Item, SimpleNamespace(nodeid=nodeid, path=path)))
    return found


def test_without_env_everything_runs() -> None:
    config, hook = _config()
    items = _items({"tests/test_a.py::test_x": None})
    ci_select.select(config, items)
    assert len(items) == 1
    assert hook.deselected == []


def test_shards_split_items_without_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    nodeids = [f"tests/test_{f}.py::test_{n}" for f in "abcdefg" for n in range(ord(f) % 5 + 1)]
    kept: list[str] = []
    for index in (1, 2, 3):
        monkeypatch.setenv(ci_select.SHARD_ENV, f"{index}/3")
        config, hook = _config()
        items = _items(dict.fromkeys(nodeids))
        ci_select.select(config, items)
        assert len(items) + len(hook.deselected) == len(nodeids)
        kept.extend(item.nodeid for item in items)
        # Файл целиком в одной части.
        files = {item.nodeid.split("::")[0] for item in items}
        assert all(node.split("::")[0] not in files for node in map(_nodeid, hook.deselected))
    assert sorted(kept) == sorted(nodeids)


def _nodeid(item: object) -> str:
    return cast(pytest.Item, item).nodeid


def test_second_call_does_not_split_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_select.SHARD_ENV, "1/2")
    config, _ = _config()
    items = _items(dict.fromkeys(f"tests/test_{n}.py::test_x" for n in range(6)))
    ci_select.select(config, items)
    first = [item.nodeid for item in items]
    ci_select.select(config, items)
    assert [item.nodeid for item in items] == first


def test_docs_scope_keeps_only_doc_readers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _write(tmp_path / "tests" / "test_reader.py", 'P = "docs/QUEUE.md"\n')
    code = _write(tmp_path / "tests" / "test_code.py", "X = 1\n")
    monkeypatch.setenv(ci_select.SCOPE_ENV, "docs")
    config, hook = _config()
    items = _items({"tests/test_reader.py::test_x": reader, "tests/test_code.py::test_y": code})
    ci_select.select(config, items)
    assert [item.nodeid for item in items] == ["tests/test_reader.py::test_x"]
    assert [_nodeid(item) for item in hook.deselected] == ["tests/test_code.py::test_y"]


def test_unknown_scope_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_select.SCOPE_ENV, "doc")
    config, _ = _config()
    with pytest.raises(pytest.UsageError):
        ci_select.select(config, [])
