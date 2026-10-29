"""Тесты ops/pool.sh — пула окон (#503, правило исполнения 10).

Окно подменяется процессом `sleep`: POOL_OWNER_PID — его pid, POOL_OWNER_COMM=sleep —
имя процесса, которое скрипт считает окном Claude. Убитый `sleep` — закрытое окно:
его замки обязаны сниматься при следующем взятии, иначе брошенное окно навсегда
держит номер, турникет или заявку на ревью. GitHub (для `wait work`) — фейковый `gh`.
Здесь же замок слияния без воркера (merge-enter / merge-leave); очередь и воркер —
tests/test_pool_merge.py.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POOL_SCRIPT = REPO_ROOT / "ops" / "pool.sh"

FAKE_GH = """#!/usr/bin/env bash
echo "498 false REVIEW_REQUIRED abc123"
"""


class Window:
    """Поддельное окно: живой процесс-владелец и вызовы скрипта от его имени."""

    def __init__(self, env: dict[str, str]) -> None:
        self.process: subprocess.Popen[bytes] = subprocess.Popen(["sleep", "300"])
        self.env = {**env, "POOL_OWNER_PID": str(self.process.pid)}

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(POOL_SCRIPT), *args],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def start(self, *args: str, **extra: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", str(POOL_SCRIPT), *args],
            env={**self.env, **extra},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


@pytest.fixture
def pool_dir(tmp_path: Path) -> Path:
    return tmp_path / "pool"


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    return tmp_path / "журнал-окон.local"


@pytest.fixture
def window(tmp_path: Path, pool_dir: Path, journal: Path) -> Iterator[Callable[[], Window]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "POOL_DIR": str(pool_dir),
        "POOL_JOURNAL": str(journal),
        "POOL_OWNER_COMM": "sleep",
        "POOL_ENTER_TIMEOUT_SEC": "0",
        "POOL_MERGE_ENTER_TIMEOUT_SEC": "0",
    }
    opened: list[Window] = []

    def open_window() -> Window:
        opened.append(Window(env))
        return opened[-1]

    yield open_window
    for w in opened:
        w.close()


def test_windows_get_distinct_numbers_and_keep_them(window: Callable[[], Window]) -> None:
    first, second = window(), window()

    assert first.run("slot").stdout.strip() == "1"
    assert second.run("slot").stdout.strip() == "2"
    assert first.run("slot").stdout.strip() == "1"


def test_all_numbers_busy_is_exit_3(window: Callable[[], Window]) -> None:
    window().run("slot", POOL_SLOTS="2")
    window().run("slot", POOL_SLOTS="2")

    result = window().run("slot", POOL_SLOTS="2")

    assert result.returncode == 3
    assert "заняты" in result.stderr


def test_closed_window_frees_its_number(window: Callable[[], Window]) -> None:
    gone = window()
    gone.run("slot")
    gone.close()

    assert window().run("slot").stdout.strip() == "1"


def test_lock_without_owner_is_never_taken_over(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    (pool_dir / "slot-1").mkdir(parents=True)

    assert window().run("slot").stdout.strip() == "2"
    assert (pool_dir / "slot-1").is_dir()


def test_turnstile_admits_one_window_at_a_time(window: Callable[[], Window]) -> None:
    first, second = window(), window()
    first.run("slot")
    second.run("slot")

    assert first.run("enter").returncode == 0
    assert second.run("enter").returncode == 4
    assert first.run("leave").returncode == 0
    assert second.run("enter").returncode == 0


def test_enter_needs_a_window_number(window: Callable[[], Window]) -> None:
    result = window().run("enter")

    assert result.returncode == 2
    assert "slot" in result.stderr


def test_turnstile_of_closed_window_is_broken(window: Callable[[], Window]) -> None:
    gone, other = window(), window()
    gone.run("slot")
    gone.run("enter")
    other.run("slot")
    gone.close()

    assert other.run("enter").returncode == 0


def test_claim_only_inside_turnstile_and_only_once(window: Callable[[], Window]) -> None:
    first, second = window(), window()
    first.run("slot")
    second.run("slot")

    assert first.run("claim", "review-498").returncode == 2
    first.run("enter")
    assert first.run("claim", "review-498").returncode == 0
    first.run("leave")
    second.run("enter")
    taken = second.run("claim", "review-498")

    assert taken.returncode == 1
    assert "окном 1" in taken.stderr


def test_claim_of_closed_window_can_be_retaken(window: Callable[[], Window]) -> None:
    gone, other = window(), window()
    gone.run("slot")
    gone.run("enter")
    gone.run("claim", "rework-498")
    gone.close()
    other.run("slot")
    other.run("enter")

    assert other.run("claim", "rework-498").returncode == 0


@pytest.mark.parametrize("name", ["review-../x", "review-", "deploy-1", "../slot-1"])
def test_claim_rejects_names_outside_the_pool(
    window: Callable[[], Window], pool_dir: Path, name: str
) -> None:
    w = window()
    w.run("slot")
    w.run("enter")

    assert w.run("claim", name).returncode == 2
    assert sorted(p.name for p in pool_dir.iterdir()) == ["ask", "slot-1", "turnstile"]


def test_release_removes_only_own_locks(window: Callable[[], Window], pool_dir: Path) -> None:
    first, second = window(), window()
    first.run("slot")
    first.run("enter")
    first.run("claim", "merge-498")
    second.run("slot")

    assert first.run("release").returncode == 0
    assert sorted(p.name for p in pool_dir.iterdir()) == ["ask", "slot-2"]


def test_free_takes_only_pool_lock_names(window: Callable[[], Window], pool_dir: Path) -> None:
    w = window()
    w.run("slot")

    assert w.run("free", "../ask").returncode == 2
    assert w.run("free", "slot-1").returncode == 0
    assert not (pool_dir / "slot-1").exists()


def test_journal_entry_has_window_number_and_three_lines(
    window: Callable[[], Window], journal: Path
) -> None:
    w = window()
    w.run("slot")

    w.run("journal", "PR #498 — ждёт ревью", "Проверил оплату заказа", "ничего")

    lines = journal.read_text().splitlines()
    assert lines[0].endswith("· Окно 1 · PR #498 — ждёт ревью")
    assert lines[1:] == ["Сделано: Проверил оплату заказа", "От тебя: ничего", ""]


def test_second_dispatcher_is_refused(window: Callable[[], Window]) -> None:
    assert window().run("dispatcher").returncode == 0
    assert window().run("dispatcher").returncode == 1


def test_status_shows_windows_dispatcher_and_open_questions(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    worker, dispatcher = window(), window()
    worker.run("slot")
    dispatcher.run("dispatcher")
    (pool_dir / "ask" / "1-498-price.md").write_text("Вопрос")
    (pool_dir / "ask" / "1-498-done.md").write_text("Вопрос")
    (pool_dir / "ask" / "1-498-done.answer").write_text("Ответ")

    out = worker.run("status").stdout

    assert "окно 1: занято" in out
    assert "окно 2: свободно" in out
    assert "диспетчер: работает" in out
    assert "вопрос без ответа: 1-498-price.md" in out
    assert "1-498-done" not in out


def test_wait_answer_returns_when_answer_appears(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    w, dispatcher = window(), window()
    w.run("slot")
    dispatcher.run("dispatcher")
    waiting = w.start("wait", "answer", "1-498-price", POOL_WAIT_INTERVAL_SEC="1")
    assert waiting.stdout is not None
    assert "жду" in waiting.stdout.readline()
    (pool_dir / "ask" / "1-498-price.answer").write_text("Да")

    out, _ = waiting.communicate(timeout=10)

    assert waiting.returncode == 0
    assert "ответ пришёл" in out


def test_wait_answer_stops_when_dispatcher_closes(window: Callable[[], Window]) -> None:
    """Закрытый диспетчер ответа не передаст — окно не должно ждать его восемь часов."""
    w, dispatcher = window(), window()
    w.run("slot")
    dispatcher.run("dispatcher")
    waiting = w.start("wait", "answer", "1-498-price", POOL_WAIT_INTERVAL_SEC="1")
    assert waiting.stdout is not None
    assert "жду" in waiting.stdout.readline()
    dispatcher.close()

    out, _ = waiting.communicate(timeout=10)

    assert "диспетчера нет" in out


def test_dispatcher_without_owner_counts_as_absent(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    """Папка диспетчера без владельца (промпт до скрипта) — не диспетчер: его место свободно."""
    (pool_dir / "dispatcher").mkdir(parents=True)
    w = window()

    assert "диспетчер: нет" in w.run("status").stdout
    assert w.run("dispatcher").returncode == 0
    assert "диспетчер: работает" in w.run("status").stdout


def test_wait_work_wakes_when_a_window_closes(window: Callable[[], Window]) -> None:
    gone, waiter = window(), window()
    gone.run("slot")
    waiting = waiter.start("wait", "work", POOL_WAIT_INTERVAL_SEC="1", POOL_WAIT_MAX_SEC="20")
    assert waiting.stdout is not None
    assert "жду" in waiting.stdout.readline()  # первый снимок снят до закрытия окна
    gone.close()

    out, _ = waiting.communicate(timeout=15)

    assert "изменилось" in out


def test_wait_ask_times_out_without_change(window: Callable[[], Window]) -> None:
    out = window().run("wait", "ask", POOL_WAIT_INTERVAL_SEC="0", POOL_WAIT_MAX_SEC="0").stdout

    assert "время вышло" in out


def pretend_worker(pool_dir: Path, owner: Window) -> Path:
    """Живой воркер слияния: замок, владелец и pid — живой процесс."""
    lock = pool_dir / "merge-worker"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{owner.process.pid}\n")
    (lock / "pid").write_text(f"{owner.process.pid}\n")
    return lock


def test_merging_lock_admits_one_window_at_a_time(window: Callable[[], Window]) -> None:
    first, second = window(), window()
    first.run("slot")
    second.run("slot")

    assert first.run("merge-enter").returncode == 0
    assert second.run("merge-enter").returncode == 4
    assert second.run("merge-leave").returncode == 1
    assert first.run("merge-leave").returncode == 0
    assert second.run("merge-enter").returncode == 0


def test_windows_do_not_merge_while_worker_runs(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    w = window()
    w.run("slot")
    pretend_worker(pool_dir, window())

    result = w.run("merge-enter")

    assert result.returncode == 1
    assert "enqueue" in result.stderr


def test_merging_lock_of_closed_window_is_broken(window: Callable[[], Window]) -> None:
    gone, other = window(), window()
    gone.run("slot")
    gone.run("merge-enter")
    other.run("slot")
    gone.close()

    assert other.run("merge-enter").returncode == 0


def test_merging_lock_held_over_45_minutes_is_broken(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    holder, other = window(), window()
    holder.run("slot")
    holder.run("merge-enter")
    other.run("slot")
    old = time.time() - 46 * 60
    os.utime(pool_dir / "merging", (old, old))

    assert other.run("merge-enter").returncode == 0


def test_worker_with_dead_process_counts_as_absent(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    """Владелец (окно-диспетчер) жив, а процесс воркера умер — окна мержат сами."""
    lock = pretend_worker(pool_dir, window())
    dead = subprocess.Popen(["true"])
    dead.wait()
    (lock / "pid").write_text(f"{dead.pid}\n")
    w = window()
    w.run("slot")

    assert "воркер слияния: нет" in w.run("status").stdout
    assert w.run("merge-enter").returncode == 0
