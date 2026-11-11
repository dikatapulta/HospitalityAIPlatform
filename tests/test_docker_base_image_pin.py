"""Базовый образ под присмотром Dependabot (#186): три файла держат один минор Python.

Блок `docker` в `.github/dependabot.yml` работает, только пока соблюдены два условия,
и оба ломаются молча — файл выглядит так же, а робот просто перестаёт приходить:

* `FROM` в `ops/Dockerfile` несёт дайджест. Минор Python закрыт игнором, и голый тег
  `3.12-slim` двигать нечем: без `@sha256:` блок — no-op;
* граница игнора — следующий минор после тега. Поднял тег, а границу забыл — игнор
  глушит уже текущий минор целиком.

Третье — минор образа равен минору, на котором CI гоняет тесты (`python-version`
в `ci.yml`): иначе зелёный `check` говорит о Python, которого в образе нет.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _ROOT / "ops" / "Dockerfile"
_DEPENDABOT = _ROOT / ".github" / "dependabot.yml"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"

_FROM_PYTHON = re.compile(
    r"^FROM python:(?P<major>\d+)\.(?P<minor>\d+)-slim(?P<digest>@sha256:[0-9a-f]{64})? AS base$",
    re.MULTILINE,
)


def _image_python() -> tuple[int, int, bool]:
    found = _FROM_PYTHON.findall(_DOCKERFILE.read_text(encoding="utf-8"))
    assert len(found) == 1, f"в ops/Dockerfile ожидался один FROM python…-slim AS base: {found}"
    major, minor, digest = found[0]
    return int(major), int(minor), bool(digest)


def _docker_update() -> dict[str, Any]:
    config = yaml.safe_load(_DEPENDABOT.read_text(encoding="utf-8"))
    blocks = [u for u in config["updates"] if u["package-ecosystem"] == "docker"]
    assert [u["directory"] for u in blocks] == ["/ops"], blocks
    block: dict[str, Any] = blocks[0]
    return block


def _ci_python_versions() -> set[str]:
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    versions: set[str] = set()
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            version = (step.get("with") or {}).get("python-version")
            if version is not None:
                versions.add(str(version))
    return versions


def test_base_image_is_pinned_by_digest() -> None:
    _, _, has_digest = _image_python()
    assert has_digest, (
        "FROM python в ops/Dockerfile без @sha256: — минор закрыт игнором, двигать роботу "
        "нечего, блок docker в dependabot.yml стоит впустую (#186)"
    )


def test_dependabot_ignore_starts_at_next_python_minor() -> None:
    major, minor, _ = _image_python()
    entries = [e for e in _docker_update().get("ignore", []) if e["dependency-name"] == "python"]
    assert entries == [{"dependency-name": "python", "versions": [f">= {major}.{minor + 1}"]}], (
        f"образ на Python {major}.{minor}: игнор робота — ровно `>= {major}.{minor + 1}`, "
        f"нашлось {entries}"
    )


def test_ci_tests_on_the_image_python_minor() -> None:
    major, minor, _ = _image_python()
    versions = _ci_python_versions()
    # Защита от пустого открытия: сломанный разбор дал бы зелёный тест впустую.
    assert versions, "в .github/workflows/ci.yml не найдено ни одного python-version"
    assert versions == {f"{major}.{minor}"}, (
        f"образ на Python {major}.{minor}, а CI гоняет тесты на {sorted(versions)}"
    )
