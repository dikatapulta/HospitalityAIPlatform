"""Ворота повторного approve конвейера слияния (ops/pool-merge.sh) — доработка ревью PR #522.

Скрипт ставит approve аккаунтом ревьюера, только если голова — одобренная с влитым
main и ничего больше. Сверка — по деревьям git: patch-id слеп к пробелам и к переносу
строки между местами с одинаковым контекстом (Б-1). Человека скрипт не перекрывает:
черновик, request changes и снятый approve останавливают PR (Б-2). Без воркера то же
делает merge-one в окне — ручной сверки и ручного approve нет (Б-3).
Стенд — из tests/test_pool_merge.py.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests import test_pool_merge as bench
from tests.test_pool_merge import (
    FAST,
    PR,
    REVIEWER,
    GitHub,
    Repo,
    Window,
    enqueue,
    pretend_worker,
    queue_entries,
    run_worker,
)

# Фикстуры стенда — те же, что у tests/test_pool_merge.py.
env = bench.env
pool_dir = bench.pool_dir
repo = bench.repo
github = bench.github
window = bench.window


def lines(*rows: str) -> str:
    return "".join(f"{row}\n" for row in rows)


def patch_id(repo: Repo, head: str, mode: str) -> str:
    """patch-id собственного diff PR — то, чем сверял прежний воркер."""
    diff = repo.git("diff", "--full-index", f"origin/main...{head}") + "\n"
    out = subprocess.run(
        ["git", "patch-id", mode],
        input=diff,
        cwd=repo.work,
        env=repo.env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()[0]


def queued_after_push(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub, files: list[str]
) -> tuple[str, str]:
    """main с файлом files[0]; approve на files[1]; после enqueue автор пушит files[2]."""
    repo.set_main(repo.change(repo.main(), {"code.py": files[0]}))
    approved = repo.change(repo.main(), {"code.py": files[1]})
    repo.set_pr_head(approved)
    github.pr(head=approved)
    enqueue(window, pool_dir)
    pushed = repo.change(approved, {"code.py": files[2]})
    repo.set_pr_head(pushed)
    github.pr(head=pushed, decision="REVIEW_REQUIRED")
    return approved, pushed


def assert_sent_back_for_review(github: GitHub, pool_dir: Path) -> None:
    assert "нужен повторный проход" in github.comments()
    assert github.calls("api") == []
    assert github.calls("pr", "merge") == []
    assert queue_entries(pool_dir) == []


def test_reindent_after_approve_needs_another_review(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    """`return` вынесен из-под `if`: patch-id --stable тот же, смысл кода — другой."""
    approved, pushed = queued_after_push(
        window,
        pool_dir,
        repo,
        github,
        [
            lines("def f(x):", "    if x:", "        log(x)", "    return 2"),
            lines("def f(x):", "    if x:", "        log(x)", "        return 1", "    return 2"),
            lines("def f(x):", "    if x:", "        log(x)", "    return 1", "    return 2"),
        ],
    )
    assert patch_id(repo, approved, "--stable") == patch_id(repo, pushed, "--stable")

    run_worker(window)

    assert_sent_back_for_review(github, pool_dir)


def test_line_moved_between_twin_functions_needs_another_review(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    """Проверка прав перенесена в соседнюю функцию с тем же контекстом ±3 строки."""
    body = ["    a = 1", "    b = 2", "    c = 3", "    d = 4", "    e = 5", "    f = 6"]

    def module(guard_in: str) -> str:
        rows: list[str] = []
        for name in ("public", "admin"):
            rows += [f"def {name}(request):", *body[:3]]
            rows += ["    require_admin(request)"] if name == guard_in else []
            rows += [*body[3:], ""]
        return lines(*rows)

    approved, pushed = queued_after_push(
        window, pool_dir, repo, github, [module(""), module("admin"), module("public")]
    )
    assert patch_id(repo, approved, "--verbatim") == patch_id(repo, pushed, "--verbatim")

    run_worker(window)

    assert_sent_back_for_review(github, pool_dir)


def test_rewritten_history_needs_another_review(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    """Rebase с force-push: содержимое то же, но голова — не потомок одобренной."""
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    github.pr(head=approved)
    enqueue(window, pool_dir)
    rebased = repo.edit_line(repo.advance_main(25, "main"), 3, "pr")
    repo.set_pr_head(rebased)
    github.pr(head=rebased, decision="REVIEW_REQUIRED")

    run_worker(window)

    assert_sent_back_for_review(github, pool_dir)


def behind_and_queued(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub, **fields: Any
) -> tuple[str, str]:
    """Одобренный PR отстал от main и стоит в очереди; update-branch приготовлен."""
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    repo.advance_main(25, "main")
    updated = repo.merge_main_into(approved)
    github.pr(head=approved, update_to=updated, **fields)
    enqueue(window, pool_dir)
    return approved, updated


def test_changes_requested_in_queue_stops_pr_before_update(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    behind_and_queued(window, pool_dir, repo, github)
    github.update(decision="CHANGES_REQUESTED", verdict="CHANGES_REQUESTED")

    run_worker(window)

    assert "request changes" in github.comments()
    assert github.calls("pr", "update-branch") == []
    assert github.calls("api") == []
    assert github.calls("pr", "merge") == []
    assert queue_entries(pool_dir) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"verdict": "CHANGES_REQUESTED", "decision": "CHANGES_REQUESTED"}, "request changes"),
        ({"draft": True}, "черновик"),
    ],
    ids=["request-changes", "draft"],
)
def test_stop_while_check_runs_is_not_overridden_by_reapprove(
    window: Callable[[], Window],
    pool_dir: Path,
    repo: Repo,
    github: GitHub,
    change: dict[str, Any],
    reason: str,
) -> None:
    """Сценарий ревью: update-branch, потом стоп человеком, потом зелёный check."""
    behind_and_queued(window, pool_dir, repo, github, checks=["pending", "success"])
    github.update(after_check=change)

    run_worker(window)

    assert len(github.calls("pr", "update-branch")) == 1
    assert reason in github.comments()
    assert github.calls("api") == []
    assert github.calls("pr", "merge") == []
    assert queue_entries(pool_dir) == []


def test_approve_dismissed_by_hand_on_approved_head_stops_pr(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head)
    enqueue(window, pool_dir)
    github.update(decision="REVIEW_REQUIRED", verdict="DISMISSED", approved="NONE")

    run_worker(window)

    assert "approve снят" in github.comments()
    assert github.calls("pr", "merge") == []


def merging_window(window: Callable[[], Window]) -> Window:
    w = window()
    w.run("slot")
    return w


def test_merge_one_updates_reapproves_and_merges_without_worker(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    repo.advance_main(25, "main")
    updated = repo.merge_main_into(approved)
    github.pr(head=approved, update_to=updated)

    result = merging_window(window).run("merge-one", str(PR), **FAST)

    assert result.returncode == 0, result.stdout + result.stderr
    [review] = github.calls("api")
    assert review["config"] == REVIEWER
    assert f"commit_id={updated}" in review["args"]
    [merge] = github.calls("pr", "merge")
    assert merge["args"][merge["args"].index("--match-head-commit") + 1] == updated
    assert github.state()["state"] == "MERGED"
    assert not (pool_dir / "merging").exists()


def test_merge_one_sends_changed_content_back_to_review(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    pushed = repo.edit_line(approved, 20, "дописал после approve")
    repo.set_pr_head(pushed)
    github.pr(head=pushed, approved=approved)  # GitHub ещё не снял approve после push

    result = merging_window(window).run("merge-one", str(PR), **FAST)

    assert result.returncode == 1
    assert "ops/pool.sh merge-one" in github.comments()
    assert_sent_back_for_review(github, pool_dir)
    assert not (pool_dir / "merging").exists()


def test_merge_one_refuses_while_worker_runs(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head)
    pretend_worker(pool_dir, window())

    result = merging_window(window).run("merge-one", str(PR), **FAST)

    assert result.returncode == 1
    assert "enqueue" in result.stderr
    assert github.calls("pr", "merge") == []
