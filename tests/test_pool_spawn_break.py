"""Обрыв безголового окна, не похожий на обрыв, и сон Mac (#1167).

27.11 Mac уснул с закрытой крышкой. Безголовое окно ждало фоновый `make check`, и `claude -p`
по своему потолку снял его через 10 минут после последнего хода, а потом вышел с кодом 0.
Сторож счёл окно закончившим и удалил его запись. Код остался не закоммиченным, заявка висела,
а в журнале не было ни строки о том, что пул стоял почти два часа.

Теперь потолок снят переменной при каждом запуске. Код 0 у окна, которое не отпустило свои
замки, сторож считает обрывом и возобновляет окно. Простой пула пишется в журнал одной записью.
Пул и фейки — tests/test_pool_spawn.py.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_pool_spawn import Pool, wait_for

CEILING = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0"


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[Pool]:
    p = Pool(tmp_path)
    yield p
    p.cleanup()


def envs(pool: Pool) -> list[str]:
    files = sorted(pool.fake.glob("calls/*.env"), key=lambda p: p.stat().st_mtime_ns)
    return [p.read_text() for p in files]


def test_window_waits_for_background_commands_on_first_run_and_on_resume(pool: Pool) -> None:
    pool.mode("exit:1")
    pool.tick()
    (worker,) = pool.ids()
    pool.wait_exit(worker)

    pool.tick()

    assert "--resume" in pool.wait_calls(2)[-1]
    wait_for(lambda: len(envs(pool)) == 2, dump=pool.dump)
    assert all(CEILING in env.splitlines() for env in envs(pool))


def test_exit_zero_with_claim_left_behind_is_resumed_as_broken(pool: Pool) -> None:
    pool.mode("claimed:left")
    pool.tick()
    (worker,) = pool.ids()
    session = pool.field(worker, "session")
    pool.wait_exit(worker)
    (pool.workers / worker / "started").write_text("0\n")  # долгий запуск, не «брать нечего»
    pool.mode("sleep")

    out = pool.tick()

    assert "оборвалось (код 0, замки не отпущены: slot-1)" in out
    args = pool.wait_calls(2)[-1]
    assert args[args.index("--resume") + 1] == session
    assert "замки не отпущены: slot-1" in args[-1], "окно узнаёт, почему его подняли"
    assert pool.field(worker, "tries") == "1"
    assert not (pool.workers / ".idle-opus").exists()


def test_exit_zero_after_release_is_finished(pool: Pool) -> None:
    pool.mode("claimed:released")
    pool.tick()
    (worker,) = pool.ids()
    pool.wait_exit(worker)
    (pool.workers / worker / "started").write_text("0\n")
    pool.mode("sleep")

    out = pool.tick()

    assert f"окно {worker}: закончило" in out
    assert worker not in pool.ids()
    assert not any("--resume" in args for args in pool.calls())


def test_mac_sleep_is_one_journal_entry_after_the_pool_wakes(pool: Pool) -> None:
    assert pool.dispatcher.run("pause").returncode == 0  # окна в этом тесте не нужны
    asleep_since = str(int(time.time()) - 3600)

    pool.tick(POOL_LAST_TICK=asleep_since)
    assert not pool.journal.exists(), "служебное пробуждение Mac — простой ещё идёт"

    out = pool.tick(POOL_LAST_TICK=asleep_since, POOL_SPAWN_TICKS="2")

    assert "Mac спал, запись в журнале" in out
    text = pool.journal.read_text()
    assert text.count("· Сторож · пул стоял 60 мин, с ") == 1
    assert "От тебя: ничего, если Mac усыпили нарочно" in text


def test_ordinary_gap_between_ticks_is_not_a_stall(pool: Pool) -> None:
    assert pool.dispatcher.run("pause").returncode == 0

    out = pool.tick(POOL_LAST_TICK=str(int(time.time()) - 120), POOL_SPAWN_TICKS="2")

    assert "пул стоял" not in out
    assert not pool.journal.exists()
