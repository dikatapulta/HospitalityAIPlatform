"""Мета-тест конфигурации pytest (блокер ревью PR #153, образец — test_import_contracts).

`testpaths` в pyproject.toml перечисляет каталоги сбора тестов явно. Тест-пакет
нового модуля/пакета (`src/**/tests`), не попавший в список, молча не собирается
ни `make check`, ни CI — зелёный прогон без единого его теста. Ровно это
случилось со `staff_portal/tests` в PR C серии 0033: 16 страничных тестов
безопасности существовали и проходили, но не исполнялись.

Тест делает требование машинным: находит все каталоги `tests` под `src/` и
сверяет каждый с `testpaths`. Канон теста: без сети и БД, только чтение файлов.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# tests/ лежит в корне репозитория — родитель этого файла есть корень.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"


def _configured_testpaths() -> list[Path]:
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    testpaths: list[str] = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    return [_REPO_ROOT / entry for entry in testpaths]


def _discover_test_dirs() -> list[Path]:
    """Каталоги `tests` под src/ с хотя бы одним файлом test_*.py."""
    return sorted(
        directory
        for directory in _SRC_DIR.rglob("tests")
        if directory.is_dir() and any(directory.glob("test_*.py"))
    )


def test_every_src_tests_dir_is_collected_by_pytest() -> None:
    discovered = _discover_test_dirs()
    # Защита от «пустого» открытия: сломанный путь дал бы зелёный тест впустую.
    assert discovered, f"Под {_SRC_DIR} не найдено ни одного каталога tests — проверьте путь."

    configured = _configured_testpaths()
    uncovered = [
        str(directory.relative_to(_REPO_ROOT))
        for directory in discovered
        if not any(directory.is_relative_to(path) for path in configured)
    ]
    assert not uncovered, (
        f"Каталоги тестов вне testpaths (pyproject.toml): {uncovered}. "
        "pytest их молча не собирает — добавьте родительский пакет в testpaths."
    )
