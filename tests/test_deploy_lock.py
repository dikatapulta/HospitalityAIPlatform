"""Один деплой за раз (issue #286) — проверка поведением.

Два `deploy.sh` внахлёст наперегонки мигрируют одну базу и переписывают
`APP_IMAGE` в `.env`, а общий маркер снимка ронял деплой, чей снимок уже лежал
на диске. Как и в `test_pre_migrate_backup.py`, настоящий `bash` гоняет
настоящий `deploy.sh` в каталоге, повторяющем `/opt/hospitality`; `docker` и
`backup.sh` подменены заглушками. Замок — flock(2): тест держит его тем же
вызовом ядра, что и `flock` в скрипте, поэтому «первый деплой» здесь — либо
сам тест, либо второй процесс `deploy.sh`.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from tests.fake_flock import install_flock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO_ROOT / "ops" / "deploy" / "deploy.sh"
_IMAGE = "ghcr.io/dikatapulta/hospitality-app:0123456789abcdef0123456789abcdef01234567"
_NEWER_IMAGE = "ghcr.io/dikatapulta/hospitality-app:fedcba9876543210fedcba9876543210fedcba98"

# Каждый вызов — строкой «образ команда» в общий лог. FAKE_HOLD_DIR: миграция
# встаёт и ждёт файла go — так деплой стоит посреди прогона, пока тест смотрит.
_FAKE_DOCKER = """#!/bin/sh
echo "$APP_IMAGE $*" >> "$DOCKER_LOG"
case "$*" in
    *"alembic upgrade head"*)
        if [ -n "${FAKE_HOLD_DIR:-}" ]; then
            : > "$FAKE_HOLD_DIR/held"
            while [ ! -e "$FAKE_HOLD_DIR/go" ]; do sleep 0.05; done
        fi
        ;;
esac
exit 0
"""

# Снимок — сразу файлом, без pg_dump: здесь важен маркер, а не дамп. NEIGHBOUR_DIR:
# пока наш деплой ждёт проверки снимка, целиком проходит деплой другого каталога
# с тем же каталогом снимков (BACKUP_DIR наследуется) — его маркер ложится рядом
# с нашим и убирается на его выходе.
_BACKUP_STUB = """#!/bin/sh
echo "$APP_IMAGE backup.sh $*" >> "$DOCKER_LOG"
printf 'age-encryption.org/v1\\n' > "$BACKUP_DIR/$1-20260818T150413Z.dump.age" || exit 1
if [ -n "${NEIGHBOUR_DIR:-}" ]; then
    neighbour="$NEIGHBOUR_DIR"
    unset NEIGHBOUR_DIR
    bash "$neighbour/deploy.sh" "$NEIGHBOUR_IMAGE" || exit 1
fi
"""

_HOLDER = "PID 4242, с 2026-11-10 23:00:00 UTC"

_ENV_KEYS_TO_DROP = (
    "APP_IMAGE",
    "BACKUP_DIR",
    "SKIP_PRE_MIGRATE_BACKUP",
    "DEPLOY_LOCK_WAIT_SECONDS",
    "FAKE_HOLD_DIR",
    "NEIGHBOUR_DIR",
    "NEIGHBOUR_IMAGE",
)


def _env_file(image: str) -> str:
    return f"APP_IMAGE={image}\nBACKUP_AGE_RECIPIENT=age1testrecipientfortestsonly\n"


def _make_server(directory: Path, bin_dir: Path) -> Path:
    directory.mkdir()
    shutil.copy(_DEPLOY_SH, directory / "deploy.sh")
    (directory / "backup.sh").write_text(_BACKUP_STUB)
    (directory / "docker-compose.staging.yml").write_text("# не читается: docker подменён\n")
    (directory / ".env").write_text(_env_file(_IMAGE))
    return directory


@pytest.fixture
def server(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)
    install_flock(bin_dir)
    return _make_server(tmp_path / "hospitality", bin_dir)


def _process_env(server: Path, env: dict[str, str] | None) -> dict[str, str]:
    process_env = dict(os.environ)
    for key in _ENV_KEYS_TO_DROP:
        process_env.pop(key, None)
    process_env["PATH"] = f"{server.parent / 'bin'}{os.pathsep}{process_env['PATH']}"
    process_env["DOCKER_LOG"] = str(server.parent / "docker.log")
    process_env["DEPLOY_LOCK_WAIT_SECONDS"] = "60"  # упавший тест не висит 20 минут
    process_env.update(env or {})
    return process_env


def _run(
    server: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "deploy.sh", *args],
        cwd=server,
        env=_process_env(server, env),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )


def _start(
    server: Path, out: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    with out.open("wb") as sink:
        return subprocess.Popen(
            ["bash", "deploy.sh", *args],
            cwd=server,
            env=_process_env(server, env),
            stdout=sink,
            stderr=subprocess.STDOUT,
        )


def _calls(server: Path) -> list[str]:
    log = server.parent / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def _wait_for(condition: Callable[[], bool], process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert process.poll() is None, f"деплой вышел раньше времени: rc={process.returncode}"
        assert time.monotonic() < deadline, "не дождались нужного шага деплоя"
        time.sleep(0.05)


@contextlib.contextmanager
def _holding_lock(server: Path) -> Iterator[None]:
    """Роль первого деплоя: замок взят, в файле — его строка, как пишет deploy.sh."""
    with (server / ".deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(_HOLDER + "\n")
        lock.flush()
        yield


def _lock_is_free(server: Path) -> bool:
    with (server / ".deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def test_running_deploy_holds_the_lock_until_exit(server: Path) -> None:
    """Деплой посреди миграции держит замок и пишет в него себя; вышел — отпустил."""
    hold = server.parent / "hold"
    hold.mkdir()
    process = _start(server, server.parent / "first.out", _IMAGE, env={"FAKE_HOLD_DIR": str(hold)})
    try:
        _wait_for((hold / "held").exists, process)
        assert not _lock_is_free(server), "второй деплой прошёл бы мимо замка"
        assert f"PID {process.pid}," in (server / ".deploy.lock").read_text()
    finally:
        (hold / "go").touch()
        assert process.wait(timeout=120) == 0
    assert _lock_is_free(server)


def test_second_deploy_waits_and_reads_env_after_the_first(server: Path) -> None:
    """Второй ждёт до первого шага; без аргумента берёт образ, записанный первым."""
    out = server.parent / "second.out"
    with _holding_lock(server):
        process = _start(server, out)  # без аргумента: образ — из .env
        try:
            _wait_for(lambda: "Жду" in out.read_text(), process)
            assert _HOLDER in out.read_text(), "строка «Жду» не называет, чей замок"
            assert _calls(server) == [], "ждущий деплой уже что-то делал"
            # Первый деплой закончился и записал в .env свою здоровую версию.
            (server / ".env").write_text(_env_file(_NEWER_IMAGE))
        except BaseException:
            process.kill()
            raise
    assert process.wait(timeout=120) == 0, out.read_text()
    assert f"Деплой образа: {_NEWER_IMAGE}" in out.read_text()
    log = "\n".join(_calls(server))
    assert _NEWER_IMAGE in log and _IMAGE not in log, "катился образ из .env до ожидания"


def test_lock_held_too_long_refuses_before_any_step(server: Path) -> None:
    with _holding_lock(server):
        result = _run(server, _IMAGE, env={"DEPLOY_LOCK_WAIT_SECONDS": "1"})

    assert result.returncode != 0
    assert _calls(server) == [], "ни снимка, ни миграции без замка"
    assert "не начат" in result.stderr
    assert _HOLDER in result.stdout


def test_neighbour_deploy_with_shared_snapshot_dir_keeps_our_marker(server: Path) -> None:
    """Сосед с общим BACKUP_DIR кладёт и убирает свой маркер, пока наш ждёт проверки.

    С общим именем маркера сосед переписал или удалил бы наш, и деплой встал бы
    со снимком на диске и чужой причиной в логе (issue #286, варианты А и Б).
    """
    neighbour = _make_server(server.parent / "neighbour", server.parent / "bin")
    snapshots = server.parent / "backups"
    env = {
        "BACKUP_DIR": str(snapshots),
        "NEIGHBOUR_DIR": str(neighbour),
        "NEIGHBOUR_IMAGE": _NEWER_IMAGE,
    }

    result = _run(server, _IMAGE, env=env)

    assert result.returncode == 0, result.stderr
    assert sum("alembic upgrade head" in call for call in _calls(server)) == 2
    assert sorted(path.name for path in snapshots.iterdir()) == [
        "pre-migrate-0123456789ab-20260818T150413Z.dump.age",
        "pre-migrate-fedcba987654-20260818T150413Z.dump.age",
    ], "маркер прогона остался в каталоге снимков"


@pytest.mark.skipif(os.geteuid() == 0, reason="root пишет и в каталог 0500")
def test_snapshot_dir_not_ready_stops_before_backup(server: Path) -> None:
    """Маркер не создать — стоп до backup.sh и миграции (путь без теста до #286)."""
    snapshots = server / "backups"
    snapshots.mkdir(mode=0o500)
    try:
        result = _run(server, _IMAGE)
    finally:
        snapshots.chmod(0o700)

    assert result.returncode != 0
    assert "не готов" in result.stderr
    assert not any("backup.sh" in call or "alembic" in call for call in _calls(server))


def test_ci_deploy_job_runs_one_at_a_time() -> None:
    """Деплои из CI выстраивает GitHub: группа на job, начатый не отменяется."""
    workflow = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    concurrency = workflow["jobs"]["deploy-staging"]["concurrency"]

    assert concurrency["cancel-in-progress"] is False
    assert "'deploy-staging'" in concurrency["group"]
