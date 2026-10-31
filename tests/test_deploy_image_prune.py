"""Чистка образов в конце деплоя (issue #380) — проверка поведением.

До #380 `ops/deploy/deploy.sh` звал `docker image prune -f` без `-a`: так
удаляются только безымянные образы, а каждая версия приходит с тегом `<sha>`.
Образы копились, и 31.10.2026 диск staging заполнился — база перестала писать.

Как и в `test_pre_migrate_backup.py`, настоящий `bash` гоняет настоящий
`deploy.sh` в каталоге, повторяющем `/opt/hospitality`, а `docker` подменён
заглушкой в `PATH`. Снимок БД здесь выключен рычагом `SKIP_PRE_MIGRATE_BACKUP`:
его проверяет соседний файл, а этому тесту нужен только путь до конца деплоя.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO_ROOT / "ops" / "deploy" / "deploy.sh"
_IMAGE = "ghcr.io/dikatapulta/hospitality-app:0123456789abcdef0123456789abcdef01234567"

# Заглушка docker: пишет каждый вызов в лог и отвечает на prune так же, как
# настоящий. FAKE_PRUNE_FAIL=1 — демон отказал в чистке.
_FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$*" in
    "image prune"*)
        if [ "${FAKE_PRUNE_FAIL:-}" = "1" ]; then
            echo "Error response from daemon: a prune operation is already running" >&2
            exit 1
        fi
        printf 'Deleted Images:\nuntagged: ghcr.io/x/app:old\n\n'
        printf 'Total reclaimed space: 3.4GB\n'
        ;;
esac
exit 0
"""

_ENV_FILE = "BACKUP_AGE_RECIPIENT=age1testrecipientfortestsonly\n"

_ENV_KEYS_TO_DROP = ("APP_IMAGE", "BACKUP_DIR", "FAKE_PRUNE_FAIL")


@pytest.fixture
def server(tmp_path: Path) -> Path:
    shutil.copy(_DEPLOY_SH, tmp_path / "deploy.sh")
    (tmp_path / "docker-compose.staging.yml").write_text("# не читается: docker подменён\n")
    (tmp_path / ".env").write_text(_ENV_FILE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    return tmp_path


def _deploy(server: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    for key in _ENV_KEYS_TO_DROP:
        process_env.pop(key, None)
    process_env["PATH"] = f"{server / 'bin'}{os.pathsep}{process_env['PATH']}"
    process_env["DOCKER_LOG"] = str(server / "docker.log")
    process_env["SKIP_PRE_MIGRATE_BACKUP"] = "1"
    process_env.update(env or {})
    return subprocess.run(
        ["bash", "deploy.sh", _IMAGE],
        cwd=server,
        env=process_env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )


def _calls(server: Path) -> list[list[str]]:
    return [line.split() for line in (server / "docker.log").read_text().splitlines()]


def _index(calls: list[list[str]], *words: str) -> int:
    """Номер первого вызова docker, где есть все слова; -1 — вызова не было."""
    for number, call in enumerate(calls):
        if all(word in call for word in words):
            return number
    return -1


def test_deploy_prunes_tagged_images_after_stack_is_healthy(server: Path) -> None:
    result = _deploy(server)

    assert result.returncode == 0, result.stderr
    calls = _calls(server)
    prune = _index(calls, "image", "prune")
    assert prune >= 0, "деплой не чистил образы вовсе"
    # Без -a prune не трогает образы с тегом — а тег есть у каждой версии.
    assert {"-a", "--all"} & set(calls[prune]), calls[prune]
    # Чистка — последний вызов docker, после подъёма стека и smoke: до этого
    # образ новой версии может ещё не быть занят контейнером.
    assert prune == len(calls) - 1, calls
    assert _index(calls, "exec", "app") >= 0, "smoke не запускался"
    # Освобождённое место видно в логе деплоя.
    assert "Total reclaimed space: 3.4GB" in result.stdout


def test_failed_prune_warns_and_keeps_healthy_deploy_green(server: Path) -> None:
    result = _deploy(server, env={"FAKE_PRUNE_FAIL": "1"})

    assert result.returncode == 0, result.stderr
    assert "чистка образов не прошла" in result.stderr
    assert "a prune operation is already running" in result.stderr
    assert "OK: staging здоров" in result.stdout
