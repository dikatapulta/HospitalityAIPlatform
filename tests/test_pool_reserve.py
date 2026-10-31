"""Тесты «занять номер» ops/pool.sh (ops/pool-reserve.sh) — #568, решение основателя 30.10.

Окно, как в tests/test_pool_script.py, — процесс `sleep` (POOL_OWNER_PID, POOL_OWNER_COMM).
Git настоящий: голый «origin» с `refs/pull/N/head` и клон, где работает скрипт, — номер
считается по деревьям main и голов открытых PR. Фейковый `gh` печатает номера открытых PR
из файла `prs`, а при файле `fail` падает, как gh без сети.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POOL_SCRIPT = REPO_ROOT / "ops" / "pool.sh"

FAKE_GH = """#!/bin/sh
[ -f "$FAKE_GH_DIR/fail" ] && exit 1
cat "$FAKE_GH_DIR/prs" 2>/dev/null || true
"""

POLICY = "Нұсқа 1.3, 29.10.2026.\n\nВерсия 1.3, 29.10.2026. Политика.\n\nVersion 1.3, 29.10.2026.\n"

MAIN = {
    "alembic/versions/0048_a.py": "",
    "alembic/versions/0049_b.py": "",
    "alembic/env.py": "",
    "docs/specs/0055-a.md": "",
    "docs/specs/0056-b.md": "",
    "docs/legal/privacy-policy.md": POLICY,
}


class Repo:
    """«origin» (голый) и клон, в котором работает скрипт; PR — ссылка refs/pull/N/head."""

    def __init__(self, root: Path, env: dict[str, str], gh_dir: Path) -> None:
        self.env, self.gh_dir = env, gh_dir
        self.origin, self.work = root / "origin.git", root / "work"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], env=env, check=True
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.work)], env=env, check=True
        )
        self.base = self.commit(MAIN)
        self.git("push", "-q", "origin", "HEAD:main")

    def git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", *args], cwd=self.work, env=self.env, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def open_pr(self, number: int, files: dict[str, str]) -> None:
        """Открытый PR поверх main с файлами files — его видит `gh pr list`."""
        self.git("checkout", "-q", "--detach", self.base)
        self.git("push", "-q", "origin", f"{self.commit(files)}:refs/pull/{number}/head")
        with (self.gh_dir / "prs").open("a") as prs:
            prs.write(f"{number}\n")

    def merge_to_main(self, files: dict[str, str]) -> None:
        """В main влился PR с файлами files."""
        self.git("checkout", "-q", "--detach", self.base)
        self.base = self.commit(files)
        self.git("push", "-q", "origin", f"{self.base}:refs/heads/main")


class Window:
    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.process: subprocess.Popen[bytes] = subprocess.Popen(["sleep", "300"])
        self.env = {**env, "POOL_OWNER_PID": str(self.process.pid)}
        self.cwd = cwd

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(POOL_SCRIPT), *args],
            env=self.env,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def reserve(self, kind: str) -> str:
        result = self.run("reserve", kind)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


@pytest.fixture
def pool_dir(tmp_path: Path) -> Path:
    return tmp_path / "pool"


@pytest.fixture
def gh_dir(tmp_path: Path) -> Path:
    path = tmp_path / "gh-state"
    path.mkdir()
    return path


@pytest.fixture
def env(tmp_path: Path, pool_dir: Path, gh_dir: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(0o755)
    git_config = tmp_path / "gitconfig"
    git_config.write_text("")
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(gh_dir),
        "GIT_CONFIG_GLOBAL": str(git_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "POOL_DIR": str(pool_dir),
        "POOL_OWNER_COMM": "sleep",
    }


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str], gh_dir: Path) -> Repo:
    return Repo(tmp_path, env, gh_dir)


@pytest.fixture
def window(env: dict[str, str], repo: Repo) -> Iterator[Callable[[], Window]]:
    opened: list[Window] = []

    def open_window() -> Window:
        opened.append(Window(env, repo.work))
        return opened[-1]

    yield open_window
    for w in opened:
        w.close()


@pytest.mark.parametrize(
    ("kind", "number"), [("migration", "0050"), ("spec", "0057"), ("policy", "1.4")]
)
def test_number_follows_main(window: Callable[[], Window], kind: str, number: str) -> None:
    assert window().reserve(kind) == number


def test_same_window_keeps_its_number_and_the_next_window_gets_another(
    window: Callable[[], Window],
) -> None:
    first, second = window(), window()

    assert first.reserve("migration") == "0050"
    assert second.reserve("migration") == "0051"
    assert first.reserve("migration") == "0050"
    assert second.reserve("spec") == "0057"


def test_number_in_open_pr_is_taken_after_its_window_closed(
    window: Callable[[], Window], repo: Repo
) -> None:
    """Окно автора ушло после ready и сняло замок, а номер живёт в его PR до merge."""
    repo.open_pr(7, {"alembic/versions/0050_c.py": "", "docs/specs/0057-c.md": ""})
    repo.open_pr(8, {"docs/legal/privacy-policy.md": POLICY.replace("1.3", "1.4")})
    w = window()

    assert w.reserve("migration") == "0051"
    assert w.reserve("spec") == "0058"
    assert w.reserve("policy") == "1.5"


@pytest.mark.parametrize(
    ("merged", "renumbered"),
    [("alembic/versions/0050_mine.py", "0051"), ("alembic/versions/0051_other.py", "0052")],
)
def test_window_gets_a_new_number_once_main_caught_up(
    window: Callable[[], Window], repo: Repo, merged: str, renumbered: str
) -> None:
    """Влился свой PR серии или чужой PR с бо́льшим номером — прежний номер уже не годится."""
    w = window()
    assert w.reserve("migration") == "0050"

    repo.merge_to_main({merged: ""})

    assert w.reserve("migration") == renumbered
    assert w.reserve("migration") == renumbered


def test_closed_window_without_pr_frees_its_number(
    window: Callable[[], Window], pool_dir: Path
) -> None:
    gone = window()
    gone.reserve("migration")
    gone.close()

    assert window().reserve("migration") == "0050"
    assert [p.name for p in pool_dir.glob("reserved-*")] == ["reserved-migration-0050"]


def test_no_number_without_open_prs(
    window: Callable[[], Window], gh_dir: Path, pool_dir: Path
) -> None:
    """gh без сети: номер из одного main мог бы совпасть с номером из открытого PR."""
    (gh_dir / "fail").write_text("")

    result = window().run("reserve", "migration")

    assert result.returncode == 2
    assert "повтори" in result.stderr
    assert not list(pool_dir.glob("reserved-*"))


@pytest.mark.parametrize("args", [(), ("deploy",), ("migration", "spec")])
def test_reserve_rejects_unknown_kinds(window: Callable[[], Window], args: tuple[str, ...]) -> None:
    assert window().run("reserve", *args).returncode == 2


def test_status_shows_numbers_and_release_frees_them(window: Callable[[], Window]) -> None:
    w = window()
    w.run("slot")
    w.reserve("migration")
    w.reserve("policy")

    out = w.run("status").stdout
    assert "занят номер: миграция 0050 — окно 1, занято с" in out
    assert "занят номер: политика гостя 1.4 — окно 1" in out

    w.run("release")
    assert "занят номер" not in w.run("status").stdout
