"""Тесты конвейера слияния ops/pool.sh (ops/pool-merge.sh) — решение основателя 28.10.

Окно, как в tests/test_pool_script.py, — процесс `sleep` (POOL_OWNER_PID,
POOL_OWNER_COMM). Git настоящий: голый репозиторий — «origin» с `refs/pull/N/head`,
рядом клон, где работают скрипты, — потому что решение «содержимое не менялось»
держится на деревьях настоящего git (merge-tree). GitHub — tests/pool_fake_gh.py.
Замок слияния (merge-enter / merge-leave) — в tests/test_pool_script.py; ворота
повторного approve и merge-one — в tests/test_pool_merge_guard.py, на этом же стенде.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POOL_SCRIPT = REPO_ROOT / "ops" / "pool.sh"
REVIEWER = "/reviewer-config"
PR = 7

FAKE_GH = Path(__file__).with_name("pool_fake_gh.py")

FAST = {
    "POOL_WORKER_IDLE_MAX_SEC": "0",
    "POOL_WORKER_INTERVAL_SEC": "0",
    "POOL_CHECK_INTERVAL_SEC": "0",
    "POOL_CHECK_MAX_SEC": "5",
    "POOL_MERGE_ENTER_INTERVAL_SEC": "0",
}


class Repo:
    """«origin» (голый) и клон, в котором работают скрипты пула."""

    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.env = env
        self.origin = root / "origin.git"
        self.work = root / "work"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], env=env, check=True
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.work)], env=env, check=True
        )
        self.commit({"base.txt": "".join(f"line {i}\n" for i in range(1, 31))})
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("fetch", "-q", "origin")

    def git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", *args], cwd=self.work, env=self.env, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        for name, text in files.items():
            (self.work / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def edit_line(self, base: str, line: int, text: str) -> str:
        """Коммит поверх base: строка line файла base.txt заменена на text."""
        self.git("checkout", "-q", "--detach", base)
        lines = (self.work / "base.txt").read_text().splitlines(keepends=True)
        lines[line - 1] = f"{text}\n"
        return self.commit({"base.txt": "".join(lines)})

    def change(self, base: str, files: dict[str, str]) -> str:
        """Коммит поверх base с файлами files."""
        self.git("checkout", "-q", "--detach", base)
        return self.commit(files)

    def set_main(self, sha: str) -> str:
        self.git("push", "-q", "origin", f"{sha}:refs/heads/main")
        return self.main()

    def main(self) -> str:
        self.git("fetch", "-q", "origin")
        return self.git("rev-parse", "origin/main")

    def advance_main(self, line: int, text: str) -> str:
        return self.set_main(self.edit_line(self.main(), line, text))

    def set_pr_head(self, sha: str) -> None:
        self.git("push", "-q", "-f", "origin", f"{sha}:refs/pull/{PR}/head")

    def merge_main_into(self, head: str) -> str:
        """Голова после «update-branch»: main, влитый в ветку PR, — как делает GitHub."""
        self.git("checkout", "-q", "--detach", head)
        self.git("merge", "-q", "--no-edit", "origin/main")
        sha = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", f"{sha}:refs/prepared/{sha[:8]}")
        return sha


class Window:
    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.process: subprocess.Popen[bytes] = subprocess.Popen(["sleep", "300"])
        self.env = {**env, "POOL_OWNER_PID": str(self.process.pid)}
        self.cwd = cwd

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(POOL_SCRIPT), *args],
            env={**self.env, **extra},
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


class GitHub:
    """Состояние фейкового gh и журнал его вызовов."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir

    def pr(self, **fields: Any) -> None:
        """Approve живой — вердикт APPROVED на голове; иначе approve снят (DISMISSED)."""
        state = {
            "state": "OPEN",
            "draft": False,
            "decision": "APPROVED",
            "checks": ["success"],
            **fields,
        }
        live = state["decision"] == "APPROVED"
        state.setdefault("verdict", "APPROVED" if live else "DISMISSED")
        state.setdefault("approved", state["head"] if live else "NONE")
        (self.dir / f"pr-{PR}.json").write_text(json.dumps(state))

    def update(self, **fields: Any) -> None:
        """Человек поменял PR, пока тот стоит в очереди."""
        (self.dir / f"pr-{PR}.json").write_text(json.dumps({**self.state(), **fields}))

    def state(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads((self.dir / f"pr-{PR}.json").read_text())
        return result

    def calls(self, *prefix: str) -> list[dict[str, Any]]:
        path = self.dir / "calls.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        calls = [json.loads(line) for line in lines]
        return [c for c in calls if c["args"][: len(prefix)] == list(prefix)]

    def comments(self) -> str:
        return "\n".join(c["args"][-1] for c in self.calls("pr", "comment"))


@pytest.fixture
def pool_dir(tmp_path: Path) -> Path:
    return tmp_path / "pool"


@pytest.fixture
def env(tmp_path: Path, pool_dir: Path) -> dict[str, str]:
    bin_dir, state_dir = tmp_path / "bin", tmp_path / "gh-state"
    bin_dir.mkdir()
    state_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n')
    fake_gh.chmod(0o755)
    git_config = tmp_path / "gitconfig"
    git_config.write_text("")
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(state_dir),
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "POOL_DIR": str(pool_dir),
        "POOL_OWNER_COMM": "sleep",
        "POOL_REVIEWER_GH_CONFIG": REVIEWER,
        "POOL_MERGE_ENTER_TIMEOUT_SEC": "0",
    }


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Repo:
    repo = Repo(tmp_path, env)
    (tmp_path / "gh-state" / "origin").write_text(str(repo.origin))
    return repo


@pytest.fixture
def github(tmp_path: Path) -> GitHub:
    return GitHub(tmp_path / "gh-state")


@pytest.fixture
def window(env: dict[str, str], repo: Repo) -> Iterator[Callable[[], Window]]:
    opened: list[Window] = []

    def open_window() -> Window:
        opened.append(Window(env, repo.work))
        return opened[-1]

    yield open_window
    for w in opened:
        w.close()


def pretend_worker(pool_dir: Path, owner: Window) -> Path:
    """Живой воркер для enqueue и merge-enter: замок, владелец и pid — живой процесс."""
    lock = pool_dir / "merge-worker"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(f"{owner.process.pid}\n")
    (lock / "pid").write_text(f"{owner.process.pid}\n")
    return lock


def enqueue(window: Callable[[], Window], pool_dir: Path) -> subprocess.CompletedProcess[str]:
    """Поставить PR в очередь от имени окна-ревьюера, пока «воркер» жив, и убрать его."""
    lock = pretend_worker(pool_dir, window())
    result = window().run("enqueue", str(PR))
    shutil.rmtree(lock)
    return result


def run_worker(window: Callable[[], Window], **extra: str) -> subprocess.CompletedProcess[str]:
    dispatcher = window()
    dispatcher.run("dispatcher")
    return dispatcher.run("merge-worker", **{**FAST, **extra})


def queue_entries(pool_dir: Path) -> list[str]:
    queue = pool_dir / "merge-queue"
    return sorted(p.name for p in queue.iterdir()) if queue.exists() else []


def test_enqueue_without_worker_tells_window_to_merge_itself(window: Callable[[], Window]) -> None:
    result = window().run("enqueue", str(PR))

    assert result.returncode == 1
    assert "merge-one" in result.stderr


def test_enqueue_needs_live_approve(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head, decision="REVIEW_REQUIRED")

    result = enqueue(window, pool_dir)

    assert result.returncode == 1
    assert "живым approve" in result.stderr
    assert queue_entries(pool_dir) == []


def test_enqueue_records_the_approved_commit_once(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    pushed = repo.edit_line(approved, 20, "push сразу после approve")
    repo.set_pr_head(pushed)
    github.pr(head=pushed, approved=approved)  # GitHub ещё не снял approve после push

    assert enqueue(window, pool_dir).returncode == 0
    assert enqueue(window, pool_dir).returncode == 0  # повтор заменяет запись, а не дублирует

    [entry] = queue_entries(pool_dir)
    assert (pool_dir / "merge-queue" / entry).read_text().split() == [str(PR), approved]


def test_up_to_date_green_pr_is_merged_on_the_reviewers_approve(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head, checks=["pending", "success"])
    enqueue(window, pool_dir)

    result = run_worker(window)

    assert result.returncode == 0, result.stdout + result.stderr
    assert github.state()["state"] == "MERGED"
    [merge] = github.calls("pr", "merge")
    assert merge["args"][merge["args"].index("--match-head-commit") + 1] == head
    assert "--squash" in merge["args"] and "--delete-branch" in merge["args"]
    assert github.calls("api") == []  # голова та, что одобрена, — approve не повторяется
    assert github.calls("pr", "update-branch") == []
    assert queue_entries(pool_dir) == []


def test_behind_pr_is_updated_reapproved_by_reviewer_and_merged(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    repo.advance_main(25, "main")
    updated = repo.merge_main_into(approved)
    github.pr(head=approved, update_to=updated)
    enqueue(window, pool_dir)

    result = run_worker(window)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(github.calls("pr", "update-branch")) == 1
    [review] = github.calls("api")
    assert review["config"] == REVIEWER
    assert "event=APPROVE" in review["args"]
    assert f"commit_id={updated}" in review["args"]
    assert any("содержимое не менялось" in a for a in review["args"])
    [merge] = github.calls("pr", "merge")
    assert merge["args"][merge["args"].index("--match-head-commit") + 1] == updated
    assert github.state()["state"] == "MERGED"


def test_changed_content_after_approve_needs_another_review(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    github.pr(head=approved)
    enqueue(window, pool_dir)
    pushed = repo.edit_line(approved, 20, "дописал после approve")
    repo.set_pr_head(pushed)
    github.pr(head=pushed, decision="REVIEW_REQUIRED")

    run_worker(window)

    assert "нужен повторный проход" in github.comments()
    assert github.calls("pr", "merge") == []
    assert github.calls("api") == []
    assert queue_entries(pool_dir) == []


def test_conflict_with_main_requests_changes_without_update(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    github.pr(head=approved)
    enqueue(window, pool_dir)
    repo.advance_main(3, "main")

    run_worker(window)

    [review] = github.calls("api")
    assert "event=REQUEST_CHANGES" in review["args"]
    assert any("нужна доработка" in a for a in review["args"])
    assert github.calls("pr", "update-branch") == []
    assert github.calls("pr", "merge") == []
    assert queue_entries(pool_dir) == []


def test_red_check_is_commented_and_not_merged(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head, checks=["failure"])
    enqueue(window, pool_dir)

    run_worker(window)

    assert "красный (failure)" in github.comments()
    assert github.calls("pr", "merge") == []
    assert queue_entries(pool_dir) == []


def test_failed_reapprove_stops_worker_and_keeps_pr_queued(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub, tmp_path: Path
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    repo.advance_main(25, "main")
    github.pr(head=approved, update_to=repo.merge_main_into(approved))
    enqueue(window, pool_dir)
    (tmp_path / "gh-state" / "api_fails").write_text("")

    result = run_worker(window)

    assert result.returncode == 5
    assert "аккаунтом ревьюера" in result.stdout
    assert github.calls("pr", "merge") == []
    assert len(queue_entries(pool_dir)) == 1
    assert not (pool_dir / "merge-worker").exists()
    assert not (pool_dir / "merging").exists()


def test_worker_is_started_only_by_dispatcher(window: Callable[[], Window]) -> None:
    result = window().run("merge-worker", **FAST)

    assert result.returncode == 2
    assert "диспетчер" in result.stderr


def test_status_shows_worker_queue_and_merging(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head)
    merger = window()
    merger.run("slot")
    merger.run("merge-enter")
    pretend_worker(pool_dir, window())
    window().run("enqueue", str(PR))

    out = merger.run("status").stdout

    assert "воркер слияния: работает" in out
    assert f"очередь слияния: #{PR}" in out
    assert "слияние идёт: окно 1" in out
