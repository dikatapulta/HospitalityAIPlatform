"""Тесты хуков разрешений безголовых окон ops/pool-perm-hook.sh — #588, PR 2.

Claude Code зовёт хук с JSON события на stdin; здесь вход подаётся руками, журнал
разрешений — в игрушечном пуле в tmp. Настоящий вызов хука самим Claude Code тестом
не проверяется — его проверяет живая проба (тело PR).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "ops" / "pool-perm-hook.sh"


def run_hook(tmp: Path, event: str, payload: object | str) -> subprocess.CompletedProcess[str]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        ["bash", str(HOOK), event],
        input=stdin,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "POOL_DIR": str(tmp), "POOL_WORKER": "1031-2200"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def journal(tmp: Path) -> list[str]:
    log = tmp / "workers" / "permissions.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_classifier_denial_is_logged_and_left_standing(tmp_path: Path) -> None:
    result = run_hook(
        tmp_path,
        "denied",
        {
            "hook_event_name": "PermissionDenied",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -r /tmp/build", "description": "Clean"},
            "reason": "[Irreversible Local Destruction]",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "PermissionDenied: без JSON отказ остаётся (retry не просим)"
    [line] = journal(tmp_path)
    assert "окно 1031-2200 · отказ классификатора · Bash: rm -r /tmp/build — [Irreversible Local Destruction]" in line


def test_request_is_denied_and_put_to_the_founder_as_a_blocking_question(tmp_path: Path) -> None:
    smoke = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "make smoke"}}
    result = run_hook(tmp_path, "request", smoke)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PermissionRequest"
    assert out["decision"]["behavior"] == "deny"
    assert "interrupt" not in out["decision"], "окно продолжает работу, а не останавливается"
    [question] = (tmp_path / "ask").glob("perm-1031-2200-*.md")
    text = question.read_text(encoding="utf-8")
    assert "Окно хочет: Bash: make smoke" in text and "блокирует: да" in text
    message = out["decision"]["message"]
    assert str(question) in message and f"wait answer {question.stem}" in message
    [line] = journal(tmp_path)
    assert "к основателю · Bash: make smoke" in line

    question.write_text(text + "Окно: нужен smoke для PR #592\n", encoding="utf-8")
    again = run_hook(tmp_path, "request", smoke)
    assert str(question) in json.loads(again.stdout)["hookSpecificOutput"]["decision"]["message"]
    assert list((tmp_path / "ask").glob("*.md")) == [question], "повтор того же действия — тот же вопрос"
    assert "нужен smoke" in question.read_text(encoding="utf-8"), "дописанное окном не затёрто"
    run_hook(tmp_path, "request", {**smoke, "tool_input": {"command": "scp x host:"}})
    assert len(list((tmp_path / "ask").glob("*.md"))) == 2


def test_journal_names_file_or_url_and_cuts_long_commands(tmp_path: Path) -> None:
    run_hook(tmp_path, "denied", {"tool_name": "Write", "tool_input": {"file_path": "/x/settings.json", "content": "{}"}})
    run_hook(tmp_path, "denied", {"tool_name": "WebFetch", "tool_input": {"url": "https://example.org/a"}})
    run_hook(tmp_path, "denied", {"tool_name": "mcp__s__t", "tool_input": {"a": 1}})
    run_hook(tmp_path, "denied", {"tool_name": "Bash", "tool_input": {"command": "x" * 500}})
    write, fetch, mcp, long = journal(tmp_path)
    assert "Write: /x/settings.json —" in write and "{}" not in write
    assert "WebFetch: https://example.org/a —" in fetch
    assert 'mcp__s__t: {"a":1} —' in mcp
    assert "x" * 200 in long and "x" * 201 not in long and "…" in long


def test_multiline_command_stays_one_journal_line(tmp_path: Path) -> None:
    run_hook(tmp_path, "denied", {"tool_name": "Bash", "tool_input": {"command": "cd /x\nmake check"}, "reason": "r"})
    [line] = journal(tmp_path)
    assert "cd /x make check" in line


def test_broken_input_still_denies_and_never_fails(tmp_path: Path) -> None:
    request = run_hook(tmp_path, "request", "не json")
    assert request.returncode == 0
    assert json.loads(request.stdout)["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert run_hook(tmp_path, "denied", "").returncode == 0
    assert len(journal(tmp_path)) == 2


def test_unknown_event_is_a_call_error(tmp_path: Path) -> None:
    result = run_hook(tmp_path, "allow", {})
    assert result.returncode == 2
    assert result.stdout == "" and journal(tmp_path) == []


SETTINGS = REPO_ROOT / "ops" / "pool-worker-settings.json"


def test_worker_settings_keep_classifier_defaults_and_wire_both_hooks() -> None:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    auto = settings["autoMode"]
    # Без "$defaults" список заменяет встроенный целиком: пропали бы запреты force push
    # и утечки данных (docs Claude Code, «Configure auto mode»).
    for section in ("environment", "allow", "soft_deny"):
        assert auto[section][0] == "$defaults", section
    assert "hard_deny" not in auto
    founder = [rule for rule in auto["allow"] + auto["soft_deny"] if "#588" in rule]
    assert len(founder) == 11, "список основателя из #588: шесть «можно» и пять «только с ним»"
    commands = {
        event: [h["command"] for group in groups for h in group["hooks"]]
        for event, groups in settings["hooks"].items()
    }
    assert commands == {
        "PermissionDenied": ['bash "$POOL_HOOK" denied'],
        "PermissionRequest": ['bash "$POOL_HOOK" request'],
        "PostToolUse": ['bash "$POOL_HOOK" done'],
    }
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == "Bash"


def test_self_granted_removals_from_founder_list_are_logged_routine_is_not(tmp_path: Path) -> None:
    logged = [
        "git push origin --delete pool-588-x",
        "git -C /w push origin :pool-588-x",
        "git worktree remove -f -f .claude/worktrees/pool-1",
        "cd /w && gh issue close 588 --comment done",
    ]
    routine = [
        "git push -u origin pool-588-x",
        "gh pr ready 592",
        "gh pr ready 592 --undo",
        "gh run rerun 123 --failed",
        "gh issue comment 588 --body-file x.md",
        "git worktree list",
        "git push origin pool-588-delete-old",
    ]
    for command in logged + routine:
        result = run_hook(tmp_path, "done", {"tool_name": "Bash", "tool_input": {"command": command}})
        assert result.returncode == 0 and result.stdout == ""
    lines = journal(tmp_path)
    assert [line.split(" · Bash: ")[1].split(" — ")[0] for line in lines] == logged
    assert all("сам разрешил" in line for line in lines)
