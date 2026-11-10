"""Форма записи `ignore` в `.github/dependabot.yml` сверяется с требованием (#322).

Игнор, который ничего не игнорирует, выглядит в файле точно так же, как работающий:
PR #315 поставил `anthropic` формой `update-types`, она прошла ревью и валидацию схемы
на стороне GitHub — и не подавляла ничего (#319). Проверка GitHub смотрит схему,
а не смысл; отказ тихий, виден только красным `check` у PR робота через неделю.

Инвариант выведен из исходника dependabot-core, экосистема `pip` без лок-файла. С #305
Python-половина — экосистема `uv` с `uv.lock` (ADR-026): текущая версия у зависимости
теперь есть, и `update-types` у диапазона, вероятно, заработал бы, — но щит этого не
проверяет и держит форму, которая работает в обоих случаях:

* `update-types` раскрывается в версии только от ТЕКУЩЕЙ версии зависимости
  (`ignore_condition.rb`, `return [] unless version`). Текущая версия есть у пина —
  ровно один спецификатор `==`/`===` (`parser.py`, `version_from_req`). У диапазона
  её дорисовывает запасной путь dependabot-core#11156 — первая трёхчастная версия
  в строке требования (`update_config.rb`, `extract_base_version_from_requirement`):
  у `>=2.0.30,<2.1` это 2.0.30, у `>=0.69,<1` — ничего, и запись — тихий no-op.
  Щит пускает `update-types` только у пина: на диапазоне с трёхчастной границей
  запись сегодня работает, но молча станет no-op, стоит переписать границу двумя
  числами (`>=2.1`). Для диапазона форма — `versions`, она подставляется безусловно.
* Голая запись (без `versions` и `update-types`) — `>= 0` на всё. Осмысленна только
  у пина (`ruff`); на диапазоне молча выключила бы зависимость из обновлений целиком,
  security-обновления тоже.

Проверяется форма записи, а не поведение робота: прогнать dependabot-core у себя
нельзя. Сверяются два файла, а не подстрока, — калечение щита закреплено
параметризованными случаями ниже: пин пускает `update-types`, диапазон — нет.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_ROOT = Path(__file__).resolve().parent.parent
_DEPENDABOT = _ROOT / ".github" / "dependabot.yml"
_PYPROJECT = _ROOT / "pyproject.toml"

# Запасной путь dependabot-core#11156 — тот же образец, что в `update_config.rb`.
_THREE_PART_VERSION = re.compile(r"\d+\.\d+\.\d+")


def _is_exact_pin(requirement: Requirement) -> bool:
    specifiers = list(requirement.specifier)
    return len(specifiers) == 1 and specifiers[0].operator in {"==", "==="}


def _project_requirements(pyproject: dict[str, Any]) -> dict[str, list[Requirement]]:
    """Требования `[project]` — основные и все группы optional, по нормализованному имени."""
    project = pyproject["project"]
    lines: list[str] = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        lines.extend(group)
    by_name: dict[str, list[Requirement]] = {}
    for line in lines:
        requirement = Requirement(line)
        by_name.setdefault(canonicalize_name(requirement.name), []).append(requirement)
    return by_name


def ignore_form_violations(
    entries: list[dict[str, Any]], requirements: dict[str, list[Requirement]]
) -> list[str]:
    """Записи `ignore`, чья форма не подходит требованию зависимости; пусто — всё в порядке."""
    violations: list[str] = []
    for entry in entries:
        name = str(entry["dependency-name"])
        if "*" in name:
            violations.append(f"{name}: шаблон имени щит не разбирает — расширьте тест")
            continue
        found = requirements.get(canonicalize_name(name))
        if not found:
            violations.append(
                f"{name}: в pyproject.toml нет такого требования — опечатка или устаревшая запись"
            )
            continue
        ranges = [requirement for requirement in found if not _is_exact_pin(requirement)]
        if not ranges:
            continue
        specifier = str(ranges[0].specifier)
        if entry.get("update-types"):
            if _THREE_PART_VERSION.search(specifier):
                why = (
                    "сегодня сработает от трёхчастной границы (dependabot-core#11156), "
                    "но молча станет no-op, стоит переписать её двумя числами"
                )
            else:
                why = "текущей версии у диапазона нет, запись — тихий no-op (#319)"
            violations.append(
                f"{name} ({specifier}): update-types у не-пина — {why}; форма — versions"
            )
        elif not entry.get("versions"):
            violations.append(
                f"{name} ({specifier}): запись без versions и update-types — это `>= 0`, "
                "на диапазоне она молча выключает зависимость из обновлений целиком"
            )
    return violations


def _python_ignore_entries() -> list[dict[str, Any]]:
    config = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))
    python_updates = [u for u in config["updates"] if u["package-ecosystem"] in {"pip", "uv"}]
    # Сверяется только корневой pyproject.toml — второй Python-каталог щит не покрывает.
    assert [u["directory"] for u in python_updates] == ["/"], python_updates
    entries: list[dict[str, Any]] = python_updates[0].get("ignore", [])
    return entries


def test_dependabot_python_ignore_form_matches_requirement() -> None:
    entries = _python_ignore_entries()
    requirements = _project_requirements(tomllib.loads(_PYPROJECT.read_text(encoding="utf-8")))
    # Защита от пустого открытия: сломанный разбор дал бы зелёный тест впустую.
    assert entries, "в Python-половине .github/dependabot.yml не найдено ни одной записи ignore"
    assert "anthropic" in requirements, "разбор pyproject.toml не нашёл anthropic"

    violations = ignore_form_violations(entries, requirements)
    assert not violations, "Форма записи ignore не подходит требованию:\n" + "\n".join(violations)


def _reqs(*lines: str) -> dict[str, list[Requirement]]:
    return _project_requirements({"project": {"dependencies": list(lines)}})


@pytest.mark.parametrize(
    ("entry", "requirement", "allowed"),
    [
        # Калечение из #322: пин пускает update-types, диапазон — нет.
        (
            {"dependency-name": "anthropic", "update-types": ["version-update:semver-major"]},
            "anthropic==0.69",
            True,
        ),
        (
            {"dependency-name": "anthropic", "update-types": ["version-update:semver-major"]},
            "anthropic>=0.69,<1",
            False,
        ),
        # Трёхчастная граница: сегодня работает, но хрупко — щит требует versions.
        (
            {"dependency-name": "sqlalchemy", "update-types": ["version-update:semver-minor"]},
            "sqlalchemy[asyncio]>=2.0.30,<2.1",
            False,
        ),
        ({"dependency-name": "anthropic", "versions": [">= 1"]}, "anthropic>=0.69,<1", True),
        ({"dependency-name": "ruff"}, "ruff==0.15.20", True),
        ({"dependency-name": "ruff"}, "ruff>=0.15", False),
        ({"dependency-name": "ruff", "versions": []}, "ruff>=0.15", False),
        ({"dependency-name": "SQLAlchemy", "versions": [">= 2.1"]}, "sqlalchemy>=2.0", True),
        ({"dependency-name": "python_multipart"}, "python-multipart==0.0.9", True),
        ({"dependency-name": "anthropik", "versions": [">= 1"]}, "anthropic>=0.69,<1", False),
        ({"dependency-name": "aws*", "versions": [">= 1"]}, "anthropic>=0.69,<1", False),
    ],
)
def test_ignore_form_rule(entry: dict[str, Any], requirement: str, allowed: bool) -> None:
    violations = ignore_form_violations([entry], _reqs(requirement))
    assert (not violations) is allowed, violations


def test_update_types_needs_pin_in_every_group() -> None:
    """Зависимость в двух группах (httpx): пин в одной не спасает диапазон в другой."""
    entry = {"dependency-name": "httpx", "update-types": ["version-update:semver-major"]}
    assert ignore_form_violations([entry], _reqs("httpx==0.27.0", "httpx>=0.27"))
