"""Тесты чистки копий `ops/pool.sh clean-worktrees` (ops/pool-clean.sh) — #585.

Репозиторий игрушечный: голый «origin» и главная папка с копиями в `.claude/worktrees/`.
Живой процесс — `sleep` (рабочая папка в копии или pid в замке сессии), мёртвый — убитый
`sleep`. Копии состарены на два часа: моложе часа скрипт их не трогает. Удаление
необратимо, поэтому главное здесь — что копия с любым сомнением остаётся.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POOL_SCRIPT = REPO_ROOT / "ops" / "pool.sh"

TO_REMOVE = {"clean", "pushed-branch", "dead-session", "gone", "pool-0101-000001"}
TO_KEEP = {
    "dirty": "незакоммиченные изменения",
    "untracked": "незакоммиченные изменения",
    "ahead": "коммиты, которых нет на origin",
    "busy": "в ней работает процесс",
    "live-session": "живая сессия Claude",
    "manual-lock": "заперта: держу руками",
    "pool-0101-000000": "окно сторожа 0101-000000 ещё на учёте",
    "fresh": "меньше 60 мин",
}


def session_lock(name: str, pid: int) -> str:
    """Причина замка в том виде, в каком копию запирает Claude Code."""
    return f"claude session {name} (pid {pid} start Sat Oct 31 17:21:35 2026)"


class Repo:
    def __init__(self, root: Path, env: dict[str, str]) -> None:
        self.env = env
        self.origin, self.main = root / "origin.git", root / "main"
        self.copies = self.main / ".claude" / "worktrees"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], env=env, check=True
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.main)], env=env, check=True
        )
        (self.main / ".gitignore").write_text(".venv/\n.claude/\n")
        (self.main / "README.md").write_text("main\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("push", "-q", "origin", "HEAD:main")
        self.git("fetch", "-q", "origin")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd or self.main,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()

    def add_copy(self, name: str, branch: str | None = None) -> Path:
        path = self.copies / name
        new = ["-b", branch] if branch else ["--detach"]
        self.git("worktree", "add", "-q", *new, str(path), "origin/main")
        return path

    def commit_in(self, path: Path, file: str) -> None:
        (path / file).write_text("change\n")
        self.git("add", file, cwd=path)
        self.git("commit", "-q", "-m", file, cwd=path)

    def listed(self) -> set[str]:
        lines = self.git("worktree", "list", "--porcelain").splitlines()
        return {
            Path(line.removeprefix("worktree ")).name
            for line in lines
            if line.startswith("worktree ") and "/.claude/worktrees/" in line
        }

    def clean(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(POOL_SCRIPT), "clean-worktrees", *args],
            cwd=self.main,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    git_config = tmp_path / "gitconfig"
    git_config.write_text("")
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "POOL_DIR": str(tmp_path / "pool"),
    }


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Iterator[Repo]:
    """Копия на каждый вердикт из TO_REMOVE и TO_KEEP."""
    r = Repo(tmp_path.resolve(), env)
    processes: list[subprocess.Popen[bytes]] = []

    clean = r.add_copy("clean")
    (clean / ".venv" / "lib").mkdir(parents=True)
    (clean / ".venv" / "lib" / "big").write_text("x" * 1000)

    pushed = r.add_copy("pushed-branch", branch="feature")
    r.commit_in(pushed, "feature.txt")
    r.git("push", "-q", "origin", "feature", cwd=pushed)

    dead = subprocess.Popen(["sleep", "300"])
    dead.kill()
    dead.wait()
    r.add_copy("dead-session")
    lock = session_lock("dead-session", dead.pid)
    r.git("worktree", "lock", "--reason", lock, str(r.copies / "dead-session"))

    shutil.rmtree(r.add_copy("gone"))

    r.add_copy("pool-0101-000001")
    lock = session_lock("pool-0101-000001", dead.pid)
    r.git("worktree", "lock", "--reason", lock, str(r.copies / "pool-0101-000001"))

    (r.add_copy("dirty") / "README.md").write_text("edited\n")
    (r.add_copy("untracked") / "notes.txt").write_text("draft\n")
    r.commit_in(r.add_copy("ahead"), "local.txt")

    processes.append(subprocess.Popen(["sleep", "300"], cwd=r.add_copy("busy")))

    live = subprocess.Popen(["sleep", "300"])
    processes.append(live)
    r.add_copy("live-session")
    lock = session_lock("live-session", live.pid)
    r.git("worktree", "lock", "--reason", lock, str(r.copies / "live-session"))

    r.add_copy("manual-lock")
    r.git("worktree", "lock", "--reason", "держу руками", str(r.copies / "manual-lock"))

    r.add_copy("pool-0101-000000")
    (tmp_path / "pool" / "workers" / "0101-000000").mkdir(parents=True)
    (tmp_path / "pool" / "workers" / "0101-000000" / "want").write_text("обрыв\n")

    past = time.time() - 2 * 3600
    for path in r.copies.iterdir():
        os.utime(path, (past, past))
    r.add_copy("fresh")

    yield r
    for p in processes:
        p.kill()
        p.wait()


def verdicts(stdout: str) -> dict[str, str]:
    """Строка копии «    <вердикт>  <имя>  <причина или размер>» → {имя: «вердикт причина»}."""
    out = {}
    for line in stdout.splitlines():
        parts = line.split(maxsplit=2)
        if line.startswith("    ") and len(parts) >= 2 and parts[0] != "Удалить:":
            out[parts[1]] = f"{parts[0]} {parts[2] if len(parts) > 2 else ''}"
    return out


def test_dry_run_names_every_copy_with_its_reason_and_removes_nothing(repo: Repo) -> None:
    before = repo.listed()

    result = repo.clean()

    assert result.returncode == 0, result.stderr
    got = verdicts(result.stdout)
    assert set(got) == TO_REMOVE | set(TO_KEEP)
    for name in TO_REMOVE:
        assert got[name].startswith("удалить "), got[name]
    for name, reason in TO_KEEP.items():
        assert got[name].startswith("оставить ") and reason in got[name], (name, got[name])
    assert repo.listed() == before
    assert (repo.copies / "clean" / ".venv" / "lib" / "big").exists()
    assert "удалить 5" in result.stdout and "clean-worktrees --yes" in result.stdout


def test_yes_removes_only_copies_without_doubt_and_keeps_their_branches(repo: Repo) -> None:
    result = repo.clean("--yes")

    assert result.returncode == 0, result.stderr
    assert repo.listed() == set(TO_KEEP)
    for name in TO_REMOVE:
        assert not (repo.copies / name).exists(), name
    for name in TO_KEEP:
        assert (repo.copies / name).is_dir(), name
    assert (repo.copies / "dirty" / "README.md").read_text() == "edited\n"
    assert repo.git("rev-parse", "--verify", "-q", "refs/heads/feature")
    assert (repo.main / "README.md").read_text() == "main\n"
    assert "Удалено копий 5" in result.stdout


def test_without_process_list_nothing_is_removed(repo: Repo, tmp_path: Path) -> None:
    """lsof не ответил — живую сессию не отличить от брошенной копии: не трогать ничего."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "lsof").chmod(0o755)
    before = repo.listed()

    result = repo.clean(
        "--yes", env={**repo.env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    )

    assert result.returncode == 2
    assert "lsof" in result.stderr
    assert repo.listed() == before
    assert (repo.copies / "clean").is_dir()


def test_unknown_argument_is_refused(repo: Repo) -> None:
    result = repo.clean("--force")

    assert result.returncode == 2
    assert "clean-worktrees [--yes]" in result.stderr
