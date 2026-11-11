"""Образ для отеля (#1048): ветка починок и метка кандидата получают образ, staging — нет.

Копия отеля обновляется только на названный коммит (docs/runbooks/pilot-copy.md,
«Выпуск в отель»). `ops/release-image.sh` гоняется настоящим `bash` с подменённым
`docker`: образ, который уже есть в GHCR, не пересобирается — на копию едет тот же
артефакт, что проверяли на staging, — а сбой GHCR не превращается в пересборку.
Проводку в `ci.yml` держат структурные проверки ниже.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "ops" / "release-image.sh"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_IMAGE = "ghcr.io/dikatapulta/hospitality-app:0123456789abcdef0123456789abcdef01234567"

# FAKE_MANIFEST: exists — образ в реестре; unknown — ответ GHCR на отсутствующий тег
# (сверено с GHCR 12.11.2026); error — сбой входа.
_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$1 $2" in
    "manifest inspect")
        case "$FAKE_MANIFEST" in
            exists) echo '{"schemaVersion": 2}'; exit 0 ;;
            unknown) echo "manifest unknown" >&2; exit 1 ;;
            *) echo "unauthorized: authentication required" >&2; exit 1 ;;
        esac
        ;;
esac
exit 0
"""


def _run(
    tmp_path: Path, manifest: str, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DOCKER_LOG"] = str(log)
    env["FAKE_MANIFEST"] = manifest
    result = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_existing_image_is_not_rebuilt(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "exists", _IMAGE)

    assert result.returncode == 0, result.stderr
    assert calls == [f"manifest inspect {_IMAGE}"]


def test_missing_image_is_built_and_pushed(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "unknown", _IMAGE)

    assert result.returncode == 0, result.stderr
    assert calls == [
        f"manifest inspect {_IMAGE}",
        f"build -f ops/Dockerfile --target production -t {_IMAGE} .",
        f"push {_IMAGE}",
    ]


def test_registry_failure_stops_without_rebuild(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "error", _IMAGE)

    assert result.returncode == 1
    assert "unauthorized" in result.stderr
    assert calls == [f"manifest inspect {_IMAGE}"]


def test_image_argument_is_required(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "unknown")

    assert result.returncode == 2
    assert calls == []


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(_CI.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_push_to_fix_branch_runs_ci(workflow: dict[Any, Any]) -> None:
    # PyYAML читает ключ `on` как True (YAML 1.1).
    push = workflow[True]["push"]

    assert push["branches"] == ["main", "release/**"]
    assert "tags" not in push  # метка кандидата сама CI не запускает — только руками


def test_release_image_job_builds_for_fix_branch_and_candidate_tag(
    workflow: dict[Any, Any],
) -> None:
    job = workflow["jobs"]["release-image"]
    run_steps = " ".join(step.get("run", "") for step in job["steps"])

    assert "startsWith(github.ref, 'refs/heads/release/')" in job["if"]
    assert "startsWith(github.ref, 'refs/tags/pilot-')" in job["if"]
    assert {"check", "migrations", "dev-environment"} <= set(job["needs"])
    assert "ops/release-image.sh" in run_steps
    assert "ssh" not in run_steps  # образ — да, деплоя — нет: сервер общий с копией отеля


def test_staging_deploys_only_main(workflow: dict[Any, Any]) -> None:
    job = workflow["jobs"]["deploy-staging"]

    assert job["if"].count("github.ref == 'refs/heads/main'") == 1
    assert job["if"].strip().startswith("${{ !cancelled() && github.ref == 'refs/heads/main' &&")
    assert "github.ref != 'refs/heads/main'" in job["concurrency"]["group"]


_STATUS_FUNCTIONS = ("always()", "success()", "failure()", "cancelled()")


def _ancestors(jobs: dict[str, Any], name: str) -> set[str]:
    needs = jobs[name].get("needs", [])
    direct = {needs} if isinstance(needs, str) else set(needs)
    return direct.union(*(_ancestors(jobs, parent) for parent in direct))


def test_job_without_status_function_has_no_skippable_ancestor(
    workflow: dict[Any, Any],
) -> None:
    """Без функции статуса GitHub добавляет в `if` неявный success() по всем предкам
    цепочки needs: пропущенный предок молча пропускает и job (ревью #1070 — так
    deploy-staging не шёл ни на одном push в main после #1063)."""
    jobs = workflow["jobs"]
    for name, job in jobs.items():
        if any(function in str(job.get("if", "")) for function in _STATUS_FUNCTIONS):
            continue
        skippable = sorted(parent for parent in _ancestors(jobs, name) if "if" in jobs[parent])
        assert not skippable, f"{name}: предки с if {skippable} — нужна функция статуса"


@pytest.mark.parametrize("name", ["deploy-staging", "release-image"])
def test_artifact_jobs_require_every_need_green(workflow: dict[Any, Any], name: str) -> None:
    """С !cancelled() упавшая проверка job уже не остановит — её результат в if явно."""
    job = workflow["jobs"][name]

    assert job["if"].strip().startswith("${{ !cancelled() &&")
    for need in job["needs"]:
        assert f"needs.{need}.result == 'success'" in job["if"], need
