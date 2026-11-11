"""Забор копии вне сервера и метка для алертера (issue #1061, ERR-OPS-023).

Настоящий ``bash`` гоняет настоящий ``ops/backup/fetch.sh`` в режиме агента против
подставных ``ssh``/``scp``/``age``: «сервер» — каталог во временной папке, команды ssh
исполняются локально с подменой пути. Проверяется то, на чём стоит алерт: метка ставится
только после проверки ключом и несёт время дампа, а не забора; уже скачанный дамп не
качается заново. Установка агентов (launchd) здесь не проверяется — её нет в режиме
``--agent``; plist агента забора — последним тестом.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FETCH = _ROOT / "ops" / "backup" / "fetch.sh"
_AGENT = _ROOT / "ops" / "backup" / "fetch-agent.sh"
_REMOTE_BACKUPS = "home/deploy/hospitality-pilot/backups"
_DUMP = "hospitality-20261112T031501Z.dump.age"
_HOUR = 3600

_FAKE_SSH = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do
    case "$1" in -o|-i) shift 2 ;; *@*) shift; break ;; *) shift ;; esac
done
echo "ssh $*" >> "$FAKE_LOG"
cmd="$(printf '%s' "$*" | sed "s#/home/deploy/#$FAKE_SERVER/home/deploy/#g")"
bash -c "$cmd" | sed "s#$FAKE_SERVER##g"
exit "${PIPESTATUS[0]}"
"""

_FAKE_SCP = """#!/usr/bin/env bash
paths=()
while [ $# -gt 0 ]; do
    case "$1" in -o|-i) shift 2 ;; -*) shift ;; *) paths+=("$1"); shift ;; esac
done
echo "scp ${paths[0]#*:}" >> "$FAKE_LOG"
cp -p "$FAKE_SERVER${paths[0]#*:}" "${paths[1]}"
"""

# «Расшифровка» — содержимое файла как есть: настоящий дамп начинается с PGDMP.
_FAKE_AGE = """#!/usr/bin/env bash
cat "${@: -1}"
"""


def _tool(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


def _server(tmp_path: Path, *, content: bytes, age_hours: float) -> Path:
    backups = tmp_path / "server" / _REMOTE_BACKUPS
    backups.mkdir(parents=True)
    dump = backups / _DUMP
    dump.write_bytes(content)
    moment = time.time() - age_hours * _HOUR
    os.utime(dump, (moment, moment))
    return backups


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _tool(bin_dir, "ssh", _FAKE_SSH)
    _tool(bin_dir, "scp", _FAKE_SCP)
    _tool(bin_dir, "age", _FAKE_AGE)
    key = tmp_path / "backup.key"
    key.write_text("AGE-SECRET-KEY-TEST\n")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "FAKE_SERVER": str(tmp_path / "server"),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "STAGING_HOST": "203.0.113.7",
        "STAGING_SSH_KEY": "",
        "BACKUP_AGE_KEY": str(key),
        "AGE_BIN": str(bin_dir / "age"),
        "PILOT_BACKUP_LOCAL_DIR": str(tmp_path / "local"),
    }
    return subprocess.run(
        ["bash", str(_FETCH), "pilot", "--agent"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _calls(tmp_path: Path) -> str:
    return (tmp_path / "calls.log").read_text()


def test_stamp_carries_dump_time_and_second_run_does_not_download(tmp_path: Path) -> None:
    backups = _server(tmp_path, content=b"PGDMP-test", age_hours=7)
    stamp = backups / "offsite-copy.stamp"

    first = _run(tmp_path)

    assert first.returncode == 0, first.stdout + first.stderr
    local_dump = tmp_path / "local" / _DUMP
    assert local_dump.read_bytes() == b"PGDMP-test"
    assert not list((tmp_path / "local").glob("*.part"))
    # Время метки — время дампа, а не забора: старый дамп не выглядит свежим.
    assert stamp.stat().st_mtime == (backups / _DUMP).stat().st_mtime
    assert local_dump.stat().st_mtime == (backups / _DUMP).stat().st_mtime

    stamp.unlink()
    (tmp_path / "calls.log").unlink()
    second = _run(tmp_path)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "Уже скачан" in second.stdout
    assert "scp " not in _calls(tmp_path)
    assert stamp.exists()  # проверка ключом и метка — каждый прогон


def test_undecryptable_dump_leaves_no_stamp(tmp_path: Path) -> None:
    backups = _server(tmp_path, content=b"not-a-dump", age_hours=7)

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "PGDMP" in result.stderr
    assert not (backups / "offsite-copy.stamp").exists()


def test_agent_plist_runs_installed_copy_hourly_with_absolute_tools(tmp_path: Path) -> None:
    """plist — то, что launchd исполнит без человека: копия скрипта вне ~/Desktop,
    режим агента, все входы переменными, PATH без Homebrew и абсолютный age."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "FETCH_AGENT_HOME": str(tmp_path / "agent"),
        "STAGING_HOST": "203.0.113.7",
        "STAGING_SSH_KEY": "~/.ssh/staging-key",
        "BACKUP_AGE_KEY": str(tmp_path / "backup.key"),
        "AGE_BIN": "/opt/homebrew/bin/age",
    }
    local_dir = tmp_path / "копии & логи"

    result = subprocess.run(
        ["bash", str(_AGENT), "plist", str(local_dir)],
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout)
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        str(tmp_path / "agent" / "ops" / "backup" / "fetch.sh"),
        "pilot",
        "--agent",
    ]
    assert plist["StartCalendarInterval"] == {"Minute": 37}
    assert plist["RunAtLoad"] is True
    assert plist["StandardOutPath"] == str(local_dir / "fetch-agent.log")
    assert plist["EnvironmentVariables"] == {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PILOT_BACKUP_LOCAL_DIR": str(local_dir),
        "STAGING_HOST": "203.0.113.7",
        "STAGING_SSH_USER": "deploy",
        "STAGING_SSH_KEY": str(tmp_path / ".ssh" / "staging-key"),
        "BACKUP_AGE_KEY": str(tmp_path / "backup.key"),
        "AGE_BIN": "/opt/homebrew/bin/age",
    }


def test_agent_plist_refuses_relative_age(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "STAGING_HOST": "203.0.113.7",
        "BACKUP_AGE_KEY": "/k",
        "AGE_BIN": "age",
    }

    result = subprocess.run(
        ["bash", str(_AGENT), "plist", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2 and "AGE_BIN" in result.stderr
