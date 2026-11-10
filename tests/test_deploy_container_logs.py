"""Журналы контейнеров переживают деплой (issue #412) — проверка поведением.

До #412 `up -d` на новом образе пересоздавал app, worker и alerter, и Docker
удалял их журналы вместе с контейнерами: разбор инцидента по прошлым логам
упирался в последний деплой, а staging деплоится на каждый merge.

Как и в `test_deploy_image_prune.py`, настоящий `bash` гоняет настоящий
`deploy.sh` в каталоге, повторяющем `/opt/hospitality`, а `docker` подменён
заглушкой в `PATH`. Снимок БД выключен рычагом `SKIP_PRE_MIGRATE_BACKUP` —
его проверяет `test_pre_migrate_backup.py`.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.fake_flock import install_flock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO_ROOT / "ops" / "deploy" / "deploy.sh"
_IMAGE = "ghcr.io/dikatapulta/hospitality-app:0123456789abcdef0123456789abcdef01234567"
_LOG_LINE = 'app-1  | 2026-11-03T00:00:00Z {"event": "http_request", "correlation_id": "c-412"}'

# Заглушка docker: пишет каждый вызов в лог; на `logs` печатает строку журнала.
# FAKE_LOGS_FAIL=1 — демон не отдал журнал.
_FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$*" in
    *" logs "*)
        if [ "${FAKE_LOGS_FAIL:-}" = "1" ]; then
            echo "Error response from daemon: no space left on device" >&2
            exit 1
        fi
        echo "$FAKE_LOG_LINE"
        ;;
esac
exit 0
"""

_ENV_FILE = "BACKUP_AGE_RECIPIENT=age1testrecipientfortestsonly\n"

_ENV_KEYS_TO_DROP = (
    "APP_IMAGE",
    "BACKUP_DIR",
    "CONTAINER_LOGS_DIR",
    "CONTAINER_LOGS_RETENTION_DAYS",
    "FAKE_LOGS_FAIL",
)


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
    install_flock(bin_dir)
    return tmp_path


def _deploy(server: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    for key in _ENV_KEYS_TO_DROP:
        process_env.pop(key, None)
    process_env["PATH"] = f"{server / 'bin'}{os.pathsep}{process_env['PATH']}"
    process_env["DOCKER_LOG"] = str(server / "docker.log")
    process_env["FAKE_LOG_LINE"] = _LOG_LINE
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


def _saved(server: Path) -> list[Path]:
    logs_dir = server / "logs"
    return sorted(logs_dir.glob("containers-*.log.gz")) if logs_dir.exists() else []


def test_logs_saved_before_stack_is_recreated(server: Path) -> None:
    result = _deploy(server)

    assert result.returncode == 0, result.stderr
    calls = _calls(server)
    save = next(n for n, call in enumerate(calls) if "logs" in call)
    assert {"app", "worker", "alerter"} <= set(calls[save]), calls[save]
    # Подъём всего стека — `up -d` без имени сервиса в конце (подъём одной db
    # перед миграциями журналов app не трогает). Сохранение обязано быть раньше.
    stack_up = next(n for n, call in enumerate(calls) if "up" in call and call[-1] != "db")
    assert save < stack_up, calls

    saved = _saved(server)
    assert len(saved) == 1, saved
    assert gzip.decompress(saved[0].read_bytes()).decode() == _LOG_LINE + "\n"
    assert saved[0].name in result.stdout
    # В журналах комната и chat id (PII_REGISTRY): читает только владелец.
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((server / "logs").stat().st_mode) == 0o700
    assert not list((server / "logs").glob("*.part"))


def test_retention_deletes_only_old_saved_logs(server: Path) -> None:
    logs_dir = server / "logs"
    logs_dir.mkdir()
    old = logs_dir / "containers-20261001T000000Z.log.gz"
    fresh = logs_dir / "containers-20261102T000000Z.log.gz"
    foreign = logs_dir / "incident-notes.txt"
    for path in (old, fresh, foreign):
        path.write_bytes(b"x")
    day = 24 * 3600
    now = time.time()
    os.utime(old, (now - 20 * day, now - 20 * day))
    os.utime(fresh, (now - 2 * day, now - 2 * day))
    os.utime(foreign, (now - 20 * day, now - 20 * day))

    result = _deploy(server)

    assert result.returncode == 0, result.stderr
    assert not old.exists(), "журнал старше срока не удалён"
    assert fresh.exists()
    assert foreign.exists(), "срок — только для файлов, которые кладёт деплой"
    assert len(_saved(server)) == 2  # свежий + сохранённый этим деплоем


def test_failed_save_warns_and_deploy_goes_on(server: Path) -> None:
    result = _deploy(server, env={"FAKE_LOGS_FAIL": "1"})

    assert result.returncode == 0, result.stderr
    assert "журналы контейнеров не сохранены" in result.stderr
    assert _saved(server) == [], "недописанный журнал не должен выглядеть сохранённым"
    assert not list((server / "logs").glob("*.part"))
    assert "OK: staging здоров" in result.stdout
