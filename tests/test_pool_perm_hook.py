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


def test_request_is_denied_with_the_way_to_the_founder(tmp_path: Path) -> None:
    result = run_hook(
        tmp_path,
        "request",
        {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "make smoke"}},
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PermissionRequest"
    assert out["decision"]["behavior"] == "deny"
    assert "шагу 4" in out["decision"]["message"] and "wait answer" in out["decision"]["message"]
    assert "interrupt" not in out["decision"], "окно продолжает работу, а не останавливается"
    [line] = journal(tmp_path)
    assert "к основателю · Bash: make smoke" in line


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
