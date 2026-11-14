"""Надзор за воркером слияния и сторожем (ops/pool-supervise.sh) — #1057 п. 3.

11.11 среда выполнения сняла фоновые команды окна-диспетчера, и пул стоял почти четыре часа.
`ops/pool.sh start` запускает воркер и сторожа отцепленными, каждый поднимает упавшего
соседа; воркер, остановленный кодом 5, не поднимается — основателю запись в журнале. Стенд —
tests/test_pool_spawn.py (диспетчер — процесс `sleep`), `gh` ещё отвечает на `repo view`.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path

import pytest

from tests.test_pool_spawn import FAKE_GH, Pool, wait_for

GH_WITH_REPO = FAKE_GH.replace(
    '"pr list") ;;', '"pr list") ;;\n"repo view") echo owner/repo; exit 0 ;;'
)
LOOP = {"POOL_SPAWN_TICKS": "", "POOL_SPAWN_INTERVAL_SEC": "1", "POOL_WORKER_INTERVAL_SEC": "1"}


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[Pool]:
    p = Pool(tmp_path)
    (tmp_path / "bin" / "gh").write_text(GH_WITH_REPO)
    assert p.dispatcher.run("pause").returncode == 0  # окна в этих тестах не нужны
    yield p
    p.cleanup()
    for lock in ("merge-worker", "spawner"):  # отцепленные процессы уходят вслед за диспетчером
        pid = lock_pid(p, lock)
        if pid is not None:
            wait_for(partial(gone, pid), timeout=15)


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def lock_pid(pool: Pool, lock: str) -> int | None:
    pid_file = pool.dir / lock / "pid"
    return int(pid_file.read_text()) if pid_file.exists() else None


def replaced(pool: Pool, lock: str, old: int) -> bool:
    pid = lock_pid(pool, lock)
    return pid is not None and pid != old


def test_start_is_only_for_the_dispatcher(pool: Pool) -> None:
    result = pool.window().run("start")

    assert result.returncode == 2
    assert "окну-диспетчеру" in result.stderr


def test_start_detaches_both_and_each_raises_the_other(pool: Pool) -> None:
    started = pool.dispatcher.run("start", **LOOP)
    assert started.returncode == 0, started.stderr
    assert "merge-worker работает отцепленно" in started.stdout
    assert "spawner" in started.stdout  # мог успеть подняться воркером — тоже надзор
    status = pool.dispatcher.run("status").stdout
    assert "воркер слияния: работает" in status and "сторож: работает" in status

    for lock in ("merge-worker", "spawner"):  # убит, как фоновая команда 11.11, — сосед поднимает
        wait_for(partial(replaced, pool, lock, 0), timeout=15)
        old = lock_pid(pool, lock)
        assert old is not None
        os.kill(old, signal.SIGKILL)
        wait_for(partial(replaced, pool, lock, old), timeout=20)

    again = pool.dispatcher.run("start", **LOOP)
    assert "merge-worker уже работает" in again.stdout and "spawner уже работает" in again.stdout


def test_worker_stopped_by_code_5_waits_for_the_founder(pool: Pool) -> None:
    (pool.dir / "merge-worker.exit").write_text("5\n")

    pool.tick(POOL_SUPERVISE="1")
    pool.tick(POOL_SUPERVISE="1")

    time.sleep(1)
    assert not (pool.dir / "merge-worker").exists()
    journal = pool.journal.read_text()
    assert journal.count("кодом 5") == 1
    assert "окну-диспетчеру: ops/pool.sh start" in journal
    assert "остановлен кодом 5" in pool.dispatcher.run("status").stdout


def test_queue_without_worker_is_flagged_and_reported_once(pool: Pool, tmp_path: Path) -> None:
    queue = pool.dir / "merge-queue"
    queue.mkdir()
    entry = queue / "20261114080000-7"
    entry.write_text("7 abc\n")
    hour_ago = time.time() - 3600
    os.utime(entry, (hour_ago, hour_ago))
    (tmp_path / "bin" / "gh").write_text(FAKE_GH)  # `repo view` не отвечает — воркер не встанет

    assert (
        "!! очередь слияния не пуста, а воркера нет 60 мин" in pool.dispatcher.run("status").stdout
    )

    pool.tick(POOL_SUPERVISE="1")
    wait_for(lambda: (pool.dir / "merge-worker.exit").exists(), timeout=15)
    pool.tick(POOL_SUPERVISE="1")

    assert pool.journal.read_text().count("воркера нет") == 1
