"""Срок жизни offsite-копий гостевой копии на машине основателя (#391).

Договор с отелем и политика обещают гостю: копия бэкапа вне сервера живёт не
дольше 14 дней. Граница — в `ops/backup/local-retention.sh`, и проверять её
надо поведением на границе, а не подстрокой: `-mtime +14` выглядит верно, а
держит файл ~15 суток (#394). Настоящий `bash` гоняет настоящий скрипт по
каталогу с файлами нужного возраста; агент launchd здесь не проверяется — он
зовёт тот же `find` с теми же аргументами (живой прогон — в PR #391).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "backup" / "local-retention.sh"
_HOUR = 3600
_DAY = 24 * _HOUR


def _file(directory: Path, name: str, age_seconds: int) -> Path:
    path = directory / name
    path.write_bytes(b"x")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_prune_deletes_by_fourteen_days_not_fifteen(tmp_path: Path) -> None:
    expired = _file(tmp_path, "hospitality-a.dump.age", 14 * _DAY)
    expired_part = _file(tmp_path, "hospitality-b.dump.age.part", 14 * _DAY + _HOUR)
    expired_plain = _file(tmp_path, "hospitality-c.dump", 20 * _DAY)
    young = _file(tmp_path, "hospitality-d.dump.age", 13 * _DAY + 22 * _HOUR)
    foreign = _file(tmp_path, "retention.log", 30 * _DAY)

    result = subprocess.run(
        ["bash", str(_SCRIPT), "prune", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not expired.exists()
    assert not expired_part.exists()
    assert not expired_plain.exists()
    assert young.exists()
    assert foreign.exists()


def test_relative_dir_is_refused(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT), "prune", "backups"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 2
