"""Тесты щита «окно пула — только в своей копии» ops/pool-copy-guard.sh — #890.

Claude Code зовёт хук PreToolUse с JSON на stdin; здесь вход подаётся руками. Окно пула —
POOL_OWNER_PID и его номер в игрушечном пуле, основная папка — POOL_MAIN_DIR в tmp
с копией в .claude/worktrees/. Настоящий вызов хука самим Claude Code тестом не
проверяется — его проверяет живая проба (тело PR).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "ops" / "pool-copy-guard.sh"
WINDOW_PID = "4242"


@dataclass
class Repo:
    main: Path
    copy: Path
    pool: Path

    def run(
        self, payload: object | str, owner: str = WINDOW_PID
    ) -> subprocess.CompletedProcess[str]:
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["bash", str(GUARD)],
            input=stdin,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                "POOL_DIR": str(self.pool),
                "POOL_MAIN_DIR": str(self.main),
                "POOL_OWNER_PID": owner,
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def bash(self, command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return self.run(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "cwd": str(cwd),
                "tool_input": {"command": command},
            }
        )

    def edit(self, file: Path, cwd: Path, tool: str = "Edit") -> subprocess.CompletedProcess[str]:
        return self.run(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool,
                "cwd": str(cwd),
                "tool_input": {"file_path": str(file)},
            }
        )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    main = (tmp_path / "main").resolve()
    copy = main / ".claude" / "worktrees" / "w1"
    (main / "src").mkdir(parents=True)
    (copy / "src").mkdir(parents=True)
    pool = tmp_path / "pool"
    (pool / "slot-2").mkdir(parents=True)
    (pool / "slot-2" / "owner").write_text(f"{WINDOW_PID}\n")
    return Repo(main=main, copy=copy, pool=pool)


def allowed(result: subprocess.CompletedProcess[str]) -> bool:
    assert result.returncode == 0, result.stderr
    return result.stdout == ""


def reason(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    return str(out["permissionDecisionReason"])


def test_window_in_main_folder_cannot_switch_its_branch(repo: Repo) -> None:
    text = reason(repo.bash("git checkout --detach origin/main", repo.main))
    assert str(repo.main) in text
    assert "EnterWorktree без имени" in text and "git worktree add --detach" in text


def test_subfolder_of_main_folder_is_the_main_folder(repo: Repo) -> None:
    assert not allowed(repo.bash("make check", repo.main / "src"))


@pytest.mark.parametrize(
    "command",
    [
        "ops/pool.sh release",
        "make pool",
        "make queue",
        "git fetch origin",
        "git worktree add --detach .claude/worktrees/w2 origin/main",
        "git worktree list",
        "pwd",
        "cd .claude/worktrees/w1",
        "cd '.claude/worktrees/w1/src'",
    ],
)
def test_window_in_main_folder_may_leave_for_its_copy(repo: Repo, command: str) -> None:
    assert allowed(repo.bash(command, repo.main))


def test_cd_by_absolute_path_into_a_copy_is_allowed(repo: Repo) -> None:
    assert allowed(repo.bash(f"cd {repo.copy}", repo.main))


@pytest.mark.parametrize(
    "command",
    [
        "cd .claude/worktrees/w1 && git checkout main",  # цепочка: хвост — в основной папке
        "git fetch origin; git checkout main",
        "ops/pool.sh status | head",
        "cd src",  # из основной папки — в основную же
        "cd .claude/worktrees",  # папка копий — не копия
        "cd /tmp",
    ],
)
def test_anything_else_in_main_folder_is_denied(repo: Repo, command: str) -> None:
    assert not allowed(repo.bash(command, repo.main))


def test_window_in_its_copy_works_freely(repo: Repo) -> None:
    assert allowed(repo.bash("git checkout -b feature && make check", repo.copy))
    assert allowed(repo.edit(repo.copy / "src" / "x.py", repo.copy / "src"))


def test_edit_of_main_folder_file_from_a_copy_points_to_the_same_file_in_the_copy(
    repo: Repo,
) -> None:
    text = reason(repo.edit(repo.main / "src" / "x.py", repo.copy / "src"))
    assert f"Тот же файл в твоей копии: {repo.copy}/src/x.py." in text


@pytest.mark.parametrize("tool", ["Write", "MultiEdit"])
def test_new_file_in_main_folder_is_denied_from_outside_the_repo(
    repo: Repo, tmp_path: Path, tool: str
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    text = reason(repo.edit(repo.main / "docs" / "new" / "note.md", scratch, tool))
    assert "EnterWorktree без имени" in text
    assert allowed(repo.edit(scratch / "shot.png", scratch, tool))
    assert allowed(repo.bash("node shot.mjs", scratch))


def test_notebook_path_counts_as_file(repo: Repo) -> None:
    result = repo.run(
        {
            "tool_name": "NotebookEdit",
            "cwd": str(repo.copy),
            "tool_input": {"notebook_path": str(repo.main / "a.ipynb")},
        }
    )
    assert not allowed(result)


def test_session_without_pool_number_is_left_alone(repo: Repo) -> None:
    founder = "777"
    assert allowed(
        repo.run(
            {
                "tool_name": "Bash",
                "cwd": str(repo.main),
                "tool_input": {"command": "git checkout main"},
            },
            founder,
        )
    )
    assert allowed(
        repo.run(
            {
                "tool_name": "Write",
                "cwd": str(repo.main),
                "tool_input": {"file_path": str(repo.main / "x.md")},
            },
            founder,
        )
    )


def test_dispatcher_is_a_pool_window(repo: Repo) -> None:
    (repo.pool / "dispatcher").mkdir()
    (repo.pool / "dispatcher" / "owner").write_text("5151\n")
    assert not allowed(
        repo.run(
            {"tool_name": "Bash", "cwd": str(repo.main), "tool_input": {"command": "git pull"}},
            "5151",
        )
    )


def test_other_tools_and_broken_input_pass(repo: Repo) -> None:
    assert allowed(
        repo.run({"tool_name": "Read", "cwd": str(repo.main), "tool_input": {"file_path": "x"}})
    )
    assert allowed(repo.run("не JSON"))
    assert allowed(repo.run({"tool_name": "Bash", "tool_input": {"command": "git checkout main"}}))


def test_project_settings_register_the_guard_for_bash_and_edits() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    [entry] = [
        e
        for e in settings["hooks"]["PreToolUse"]
        if any("ops/pool-copy-guard.sh" in h["command"] for h in e["hooks"])
    ]
    for tool in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert tool in entry["matcher"].split("|")
    assert GUARD.exists()
