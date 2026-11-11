"""Сбой сида не останавливает деплой между миграциями и подъёмом стека (#1053).

Миграции к этому шагу уже применены: остановка оставила бы старый образ работать на
новой схеме базы — и на гостевой копии отеля тоже. Как в `test_deploy_container_logs.py`,
настоящий `bash` гоняет настоящий `deploy.sh`, а `docker` подменён заглушкой в `PATH`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fake_flock import install_flock

_DEPLOY_SH = Path(__file__).resolve().parent.parent / "ops" / "deploy" / "deploy.sh"
_IMAGE = "ghcr.io/dikatapulta/hospitality-app:0123456789abcdef0123456789abcdef01234567"

# Заглушка docker: пишет каждый вызов в лог; сид падает, как упал бы на сбое базы.
_FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$*" in
    *"hospitality.tools.seed"*)
        echo "Traceback: seed failed" >&2
        exit 1
        ;;
esac
exit 0
"""


@pytest.fixture
def server(tmp_path: Path) -> Path:
    shutil.copy(_DEPLOY_SH, tmp_path / "deploy.sh")
    (tmp_path / "docker-compose.staging.yml").write_text("# не читается: docker подменён\n")
    (tmp_path / ".env").write_text("BACKUP_AGE_RECIPIENT=age1testrecipientfortestsonly\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    install_flock(bin_dir)
    return tmp_path


def test_failed_seed_warns_and_stack_still_comes_up(server: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key != "APP_IMAGE"}
    env["PATH"] = f"{server / 'bin'}{os.pathsep}{env['PATH']}"
    env["DOCKER_LOG"] = str(server / "docker.log")
    env["SKIP_PRE_MIGRATE_BACKUP"] = "1"

    result = subprocess.run(
        ["bash", "deploy.sh", _IMAGE],
        cwd=server,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "сид демо-данных упал" in result.stderr
    calls = [line.split() for line in (server / "docker.log").read_text().splitlines()]
    migrate = next(n for n, call in enumerate(calls) if "alembic" in call)
    seed = next(n for n, call in enumerate(calls) if "hospitality.tools.seed" in call)
    # Подъём всего стека — `up -d` без имени сервиса в конце (`up -d db` — до миграций).
    stack_up = next(n for n, call in enumerate(calls) if "up" in call and call[-1] != "db")
    assert migrate < seed < stack_up, calls
    assert "OK: staging здоров" in result.stdout
