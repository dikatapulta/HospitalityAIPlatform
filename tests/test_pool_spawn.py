"""Тесты сторожа безголовых окон ops/pool.sh (ops/pool-spawn.sh) — #588, PR 1.

Пул игрушечный, в tmp. Окно-диспетчер и ручные окна, как в tests/test_pool_script.py, —
процессы `sleep` (POOL_OWNER_PID, POOL_OWNER_COMM). Безголовое окно — поддельный `claude`:
пишет свои аргументы и окружение и ведёт себя по файлу mode (закончить с кодом, «брать
нечего», лимит подписки, работать до сигнала, ждать ответа). GitHub — поддельный `gh`
(снимок `wait work`), давление памяти — поддельный `sysctl`. Сторож делает один ход
за вызов (POOL_SPAWN_TICKS=1), поэтому каждое решение проверяется отдельно.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POOL_SCRIPT = REPO_ROOT / "ops" / "pool.sh"

FAKE_CLAUDE = """#!/usr/bin/env bash
mode="$(cat "$FAKE/mode" 2>/dev/null || echo exit:0)"
env >"$FAKE/calls/$$.env"
printf '%s\\n' "$@" >"$FAKE/calls/$$.args"
case "$mode" in
exit:*) exit "${mode#exit:}" ;;
idle)
    POOL_OWNER_PID=$$ bash "$FAKE_POOL_SCRIPT" journal "работа" "сделано" "ничего"
    POOL_OWNER_PID=$$ bash "$FAKE_POOL_SCRIPT" idle
    ;;
limit) echo "You've hit your session limit · resets 3:45pm"; exit 1 ;;
sleep) trap 'exit 130' INT; trap 'exit 143' TERM; sleep 300 & wait $! ;;
wait)
    trap 'exit 130' INT; trap 'exit 143' TERM
    POOL_WAIT_INTERVAL_SEC=300 bash "$FAKE_POOL_SCRIPT" wait answer 1-588-x & wait $!
    ;;
esac
"""

FAKE_GH = """#!/usr/bin/env bash
[ -f "$FAKE/gh-down" ] && exit 1
cat "$FAKE/prs" 2>/dev/null || echo "589 true REVIEW_REQUIRED abc123"
"""

FAKE_SYSCTL = """#!/usr/bin/env bash
cat "$FAKE/pressure" 2>/dev/null || echo 1
"""


# 30 с, а не 10: окно идёт под `nice -n 10`, и на занятом раннере его старт медленный.
def wait_for(
    check: Callable[[], bool], timeout: float = 30.0, dump: Callable[[], str] = str
) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"не дождался\n{dump()}")
        time.sleep(0.05)


class Window:
    """Поддельное окно Claude: живой процесс-владелец и вызовы скрипта от его имени."""

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
            timeout=60,
        )

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


class Pool:
    def __init__(self, tmp: Path) -> None:
        self.dir = tmp / "pool"
        self.fake = tmp / "fake"
        self.journal = tmp / "журнал-окон.local"
        (self.fake / "calls").mkdir(parents=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, text in {"claude": FAKE_CLAUDE, "gh": FAKE_GH, "sysctl": FAKE_SYSCTL}.items():
            (bin_dir / name).write_text(text)
            (bin_dir / name).chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "FAKE_POOL_SCRIPT": str(POOL_SCRIPT),
            "POOL_DIR": str(self.dir),
            "POOL_JOURNAL": str(self.journal),
            "POOL_OWNER_COMM": "sleep",
            "POOL_CLAUDE_BIN": str(bin_dir / "claude"),
            "POOL_SPAWN_ROOT": str(tmp),
            "POOL_SPAWN_TICKS": "1",
            "POOL_SPAWN_INTERVAL_SEC": "0",
            "POOL_STOP_GRACE_SEC": "3",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "dispatcher-session",
            "ANTHROPIC_API_KEY": "sk-test-not-for-windows",
        }
        self.windows: list[Window] = []
        self.dispatcher = self.window()
        assert self.dispatcher.run("dispatcher").returncode == 0
        self.workers = self.dir / "workers"
        self.workers.mkdir(parents=True)
        # Окно Fable в этих тестах не нужно, пока его не ждут явно: последний запуск — «сейчас».
        (self.workers / ".fable-last").write_text(f"{int(time.time())}\n")

    def window(self) -> Window:
        self.windows.append(Window(self.env))
        return self.windows[-1]

    def mode(self, mode: str) -> None:
        (self.fake / "mode").write_text(mode)

    def tick(self, **extra: str) -> str:
        result = self.dispatcher.run("spawner", **extra)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def ids(self) -> list[str]:
        return sorted(p.name for p in self.workers.iterdir() if p.is_dir())

    def field(self, worker: str, name: str) -> str:
        return (self.workers / worker / name).read_text().strip()

    def calls(self) -> list[list[str]]:
        files = sorted(self.fake.glob("calls/*.args"), key=lambda p: p.stat().st_mtime_ns)
        return [p.read_text().splitlines() for p in files]

    def wait_calls(self, n: int) -> list[list[str]]:
        wait_for(lambda: len(self.calls()) >= n, dump=self.dump)
        return self.calls()

    def wait_exit(self, worker: str) -> None:
        wait_for(lambda: (self.workers / worker / "exit").exists(), dump=self.dump)

    def wait_pid(self, worker: str) -> int:
        wait_for(lambda: (self.workers / worker / "pid").exists(), dump=self.dump)
        return int(self.field(worker, "pid"))

    def dump(self) -> str:
        """Что видно при таймауте: записи окон, их логи и процессы — для разбора падения в CI."""
        files = sorted(p for p in self.workers.rglob("*") if p.is_file())
        ps = subprocess.run(
            ["ps", "-ww", "-A", "-o", "pid=,ppid=,pgid=,stat=,args="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return "\n".join(
            [f"COLUMNS={os.environ.get('COLUMNS')!r}"]
            + [f"{p.relative_to(self.workers)}: {p.read_text(errors='replace')!r}" for p in files]
            + [
                line
                for line in ps.splitlines()
                if "sleep" in line or str(self.fake) in line or "pool" in line
            ]
        )

    def cleanup(self) -> None:
        for pid_file in self.workers.glob("*/pid"):
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
        for w in self.windows:
            w.close()


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[Pool]:
    p = Pool(tmp_path)
    yield p
    p.cleanup()


def test_window_starts_headless_in_own_copy_with_clean_environment(pool: Pool) -> None:
    out = pool.tick()

    args = pool.wait_calls(1)[0]
    (worker,) = pool.ids()
    assert "запущено" in out
    assert args[:2] == ["-p", "--model"]
    for flag, value in [
        ("--model", "opus"),
        ("--effort", "xhigh"),
        ("--permission-mode", "auto"),
        ("--permission-prompts", "none"),
        ("-w", f"pool-{worker}"),
        ("--session-id", pool.field(worker, "session")),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--bare" not in args
    assert "безголовое окно пула" in args[-1]
    env = next(pool.fake.glob("calls/*.env")).read_text()
    assert f"POOL_WORKER={worker}" in env
    for leaked in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY", "POOL_OWNER_PID"):
        assert f"{leaked}=" not in env


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("slots", "предел 1"),
        ("critical-memory", "давление памяти 4"),
        ("github", "GitHub не отвечает"),
        ("pause", "пауза"),
    ],
)
def test_no_window_unless_every_condition_holds(pool: Pool, setup: str, reason: str) -> None:
    extra: dict[str, str] = {}
    if setup == "slots":
        pool.window().run("slot", POOL_SLOTS="1")
        extra = {"POOL_SLOTS": "1"}
    elif setup == "critical-memory":
        (pool.fake / "pressure").write_text("4\n")
    elif setup == "github":
        (pool.fake / "gh-down").write_text("")
    else:
        assert pool.dispatcher.run("pause").returncode == 0

    out = pool.tick(**extra)

    assert reason in out
    assert pool.ids() == []
    assert pool.calls() == []


def test_memory_warn_still_fills_the_pool_up_to_the_floor(pool: Pool) -> None:
    """Решение основателя 31.10: до четырёх окон — и при «warn», сверх — только при «normal»."""
    pool.mode("sleep")
    (pool.fake / "pressure").write_text("2\n")
    pool.window().run("slot")

    outs = [pool.tick(POOL_MIN_WINDOWS="3") for _ in range(3)]

    assert len(pool.ids()) == 2
    assert "давление памяти 2 при 3 окнах" in outs[-1]
    (pool.fake / "pressure").write_text("1\n")
    pool.tick(POOL_MIN_WINDOWS="3")
    assert len(pool.ids()) == 3


def test_founder_cap_lowers_the_number_of_windows_and_lifts_back(pool: Pool) -> None:
    pool.mode("sleep")
    assert pool.dispatcher.run("windows", "1").returncode == 0

    pool.tick()
    out = pool.tick()

    assert len(pool.ids()) == 1
    assert "предел 1" in out
    assert "предел 1" in pool.dispatcher.run("status").stdout
    assert pool.dispatcher.run("windows", "0").returncode == 2
    assert pool.dispatcher.run("windows", "5").returncode == 0
    pool.tick()
    assert len(pool.ids()) == 2


def test_started_window_counts_before_it_takes_a_number(pool: Pool) -> None:
    pool.mode("sleep")
    pool.window().run("slot", POOL_SLOTS="2")

    pool.tick(POOL_SLOTS="2")
    out = pool.tick(POOL_SLOTS="2")

    assert len(pool.ids()) == 1
    assert "предел 2" in out


def test_window_with_nothing_to_take_is_not_restarted_until_the_pool_changes(pool: Pool) -> None:
    pool.mode("idle")
    pool.tick()
    (first,) = pool.ids()
    pool.wait_exit(first)

    out = pool.tick()

    assert "брать нечего" in out
    assert pool.ids() == []
    assert "(безголовое)" in pool.journal.read_text()
    (pool.fake / "prs").write_text("590 false APPROVED def456\n")
    pool.tick()
    assert len(pool.ids()) == 1


def test_finished_long_run_is_followed_by_a_new_window(pool: Pool) -> None:
    pool.mode("exit:0")
    pool.tick()
    (first,) = pool.ids()
    pool.wait_exit(first)
    (pool.workers / first / "started").write_text("0\n")

    pool.tick()

    assert len(pool.ids()) == 1 and pool.ids() != [first]
    assert len(pool.wait_calls(2)) == 2


def test_broken_window_is_resumed_three_times_then_dropped_and_spawner_holds(pool: Pool) -> None:
    pool.mode("exit:1")
    pool.tick()
    (worker,) = pool.ids()
    session = pool.field(worker, "session")
    for n in range(1, 4):
        pool.wait_exit(worker)
        pool.tick()
        args = pool.wait_calls(n + 1)[-1]
        assert args[args.index("--resume") + 1] == session
        assert "-w" not in args

    pool.wait_exit(worker)
    out = pool.tick()

    assert pool.ids() == []
    assert "оборвалось 3 раза" in pool.journal.read_text()
    assert "стою до" in out
    assert len(pool.calls()) == 4


def test_usage_limit_holds_spawner_and_resume_does_not_spend_a_try(pool: Pool) -> None:
    pool.mode("limit")
    pool.tick()
    (worker,) = pool.ids()
    pool.wait_exit(worker)

    out = pool.tick()

    assert "лимит подписки" in out
    assert "ждёт возобновления — лимит подписки" in pool.dispatcher.run("status").stdout
    assert len(pool.calls()) == 1
    pool.mode("sleep")
    assert pool.dispatcher.run("resume").returncode == 0
    pool.tick()
    args = pool.wait_calls(2)[-1]
    assert "--resume" in args
    assert pool.field(worker, "tries") == "0"


def test_pause_now_stops_windows_and_resume_continues_them(pool: Pool) -> None:
    pool.mode("sleep")
    pool.tick()
    (worker,) = pool.ids()
    pool.wait_pid(worker)
    pool.wait_calls(1)

    assert pool.dispatcher.run("pause", "--now").returncode == 0
    pool.wait_exit(worker)
    assert pool.field(worker, "exit") == "130"
    out = pool.tick()
    assert "пауза" in out
    assert pool.field(worker, "want") == "пауза"
    assert len(pool.calls()) == 1

    pool.dispatcher.run("resume")
    pool.tick()
    args = pool.wait_calls(2)[-1]
    assert args[args.index("--resume") + 1] == pool.field(worker, "session")


def test_make_room_stops_waiting_windows_first_then_the_youngest(pool: Pool) -> None:
    started: dict[int, str] = {}
    for n, (mode, at) in enumerate([("wait", 100), ("sleep", 200), ("sleep", 300)], start=1):
        before = set(pool.ids())
        pool.mode(mode)
        pool.tick()
        (new,) = set(pool.ids()) - before
        pool.wait_calls(n)  # окно прочло свой режим — следующий можно менять
        (pool.workers / new / "started").write_text(f"{at}\n")
        started[at] = new
    waiting = pool.wait_pid(started[100])
    # Узкий COLUMNS: без -ww Linux-ps режет длинный путь, и ждущее окно не видно (так падал CI).
    narrow = {**os.environ, "COLUMNS": "80"}

    def waits() -> bool:
        ps = subprocess.run(
            ["ps", "-ww", "-A", "-o", "ppid=,args="],
            capture_output=True,
            text=True,
            check=True,
            env=narrow,
        )
        return any(
            line.split(None, 1)[0] == str(waiting) and "wait answer 1-588-x" in line
            for line in ps.stdout.splitlines()
        )

    wait_for(waits, dump=pool.dump)

    result = pool.dispatcher.run("make-room", "2", COLUMNS="80")

    assert result.returncode == 0, result.stderr
    stopped = {w for w in pool.ids() if (pool.workers / w / "stop").exists()}
    assert stopped == {started[100], started[300]}
    assert (pool.dir / "pause").exists()


def test_fable_window_is_single_and_not_restarted_within_an_hour(pool: Pool) -> None:
    (pool.workers / ".fable-last").unlink()
    pool.mode("sleep")

    pool.tick()
    pool.tick()

    kinds = sorted(pool.field(w, "kind") for w in pool.ids())
    assert kinds == ["fable", "opus"]
    models = sorted(a[a.index("--model") + 1] for a in pool.wait_calls(2))
    assert models == ["fable", "opus"]


def test_spawner_belongs_to_dispatcher_and_stops_with_it(pool: Pool) -> None:
    refused = pool.window().run("spawner")
    assert refused.returncode == 2
    assert "окно-диспетчер" in refused.stderr

    (pool.fake / "pressure").write_text("4\n")
    spawner = subprocess.Popen(
        ["bash", str(POOL_SCRIPT), "spawner"],
        env={**pool.dispatcher.env, "POOL_SPAWN_TICKS": "", "POOL_SPAWN_INTERVAL_SEC": "1"},
        stdout=subprocess.PIPE,
        text=True,
    )
    wait_for(lambda: (pool.dir / "spawner" / "pid").exists())
    status = pool.dispatcher.run("status").stdout
    assert "сторож: работает, окон 0, предел 5, пауза нет" in status

    pool.dispatcher.close()
    out, _ = spawner.communicate(timeout=15)
    assert "сторож остановлен" in out


def test_idle_is_only_for_headless_windows(pool: Pool) -> None:
    result = pool.window().run("idle")

    assert result.returncode == 2
    assert "не безголовое окно" in result.stderr
