"""Вердикт ревью скриптом пула (ops/pool-verdict.sh) — #1058 п. 1, слово основателя 14.11; #873.

Approve аккаунтом ревьюера ставит только окно с заявкой review-<PR>, на голову, которую оно
само держало в своей копии и в которой есть свежий main, и только если ни один commit PR,
кроме своих записей ревьюера, не создан в его копии (окно-автор свой PR не одобряет).
Посторонний PR (#1059) не получает ни вердикта, ни места в очереди.
Стенд — tests/test_pool_merge.py: «origin» и клон окна-ревьюера; автор пишет PR в своём клоне.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import test_pool_merge as bench
from tests.test_pool_merge import PR, REVIEWER, GitHub, Repo, Window, enqueue, queue_entries

env = bench.env
pool_dir = bench.pool_dir
repo = bench.repo
github = bench.github
window = bench.window


def git(where: Path, repo: Repo, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=where, env=repo.env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def authored_pr(repo: Repo, tmp_path: Path, files: dict[str, str]) -> str:
    """PR, написанный в клоне автора, — в копии ревьюера этих commit'ов нет."""
    author = tmp_path / "author"
    if not author.exists():
        git(tmp_path, repo, "clone", "-q", str(repo.origin), str(author))
    git(author, repo, "fetch", "-q", "origin")
    git(author, repo, "checkout", "-q", "--detach", "origin/main")
    for name, text in files.items():
        (author / name).parent.mkdir(parents=True, exist_ok=True)
        (author / name).write_text(text)
    git(author, repo, "add", "-A")
    git(author, repo, "commit", "-q", "-m", "feature")
    head = git(author, repo, "rev-parse", "HEAD")
    git(author, repo, "push", "-q", "-f", "origin", f"{head}:refs/pull/{PR}/head")
    return head


def review_checkout(repo: Repo) -> str:
    """Первый шаг ревью: голова PR в копии ревьюера (main в ней уже есть)."""
    repo.git("fetch", "-q", "origin", f"refs/pull/{PR}/head")
    repo.git("checkout", "-q", "--detach", "FETCH_HEAD")
    return repo.git("rev-parse", "HEAD")


def reviewer(window: Callable[[], Window], *, claim: bool = True) -> Window:
    w = window()
    assert w.run("slot").returncode == 0
    if claim:
        for step in (("enter",), ("claim", f"review-{PR}"), ("leave",)):
            assert w.run(*step).returncode == 0, step
    return w


@pytest.fixture
def verdict(tmp_path: Path) -> Path:
    path = tmp_path / "verdict.md"
    path.write_text("## Ревью PR — уровень A\n\nБлокеров нет.\n")
    return path


def test_reviewer_window_approves_the_head_it_reviewed(
    window: Callable[[], Window], repo: Repo, github: GitHub, tmp_path: Path, verdict: Path
) -> None:
    head = authored_pr(repo, tmp_path, {"code.py": "x = 1\n"})
    github.pr(head=head, decision="REVIEW_REQUIRED")
    review_checkout(repo)

    result = reviewer(window).run("approve", str(PR), head, str(verdict))

    assert result.returncode == 0, result.stderr
    [call] = github.calls("api")
    assert call["config"] == REVIEWER
    assert "event=APPROVE" in call["args"] and f"commit_id={head}" in call["args"]
    [body] = [a for a in call["args"] if a.startswith("body=")]
    assert "Блокеров нет." in body and "скриптом пула" in body
    assert github.state()["approved"] == head


def test_request_changes_goes_through_the_same_claim(
    window: Callable[[], Window], repo: Repo, github: GitHub, tmp_path: Path, verdict: Path
) -> None:
    head = authored_pr(repo, tmp_path, {"code.py": "x = 1\n"})
    github.pr(head=head, decision="REVIEW_REQUIRED")

    result = reviewer(window).run("request-changes", str(PR), head, str(verdict))

    assert result.returncode == 0, result.stderr
    [call] = github.calls("api")
    assert "event=REQUEST_CHANGES" in call["args"]


def test_reviewers_own_later_notes_do_not_make_it_the_author(
    window: Callable[[], Window], repo: Repo, github: GitHub, tmp_path: Path, verdict: Path
) -> None:
    authored_pr(repo, tmp_path, {"code.py": "x = 1\n"})
    review_checkout(repo)
    (repo.work / "docs" / "later").mkdir(parents=True)
    head = repo.commit({"docs/later/2026-11-14-pr7-x.md": "потом\n"}, "later")
    repo.set_pr_head(head)
    github.pr(head=head, decision="REVIEW_REQUIRED")

    result = reviewer(window).run("approve", str(PR), head, str(verdict))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("claim", [False, True], ids=["no-claim", "author"])
def test_author_or_window_without_claim_gets_no_approve(
    window: Callable[[], Window], repo: Repo, github: GitHub, verdict: Path, claim: bool
) -> None:
    head = repo.edit_line(repo.main(), 3, "написал сам")  # commit в копии этого окна
    repo.set_pr_head(head)
    github.pr(head=head, decision="REVIEW_REQUIRED")

    result = reviewer(window, claim=claim).run("approve", str(PR), head, str(verdict))

    assert result.returncode == 1
    assert ("автор свой PR не одобряет" if claim else "заявкой review-7") in result.stderr
    assert github.calls("api") == []


@pytest.mark.parametrize("case", ["moved", "stale-main", "not-seen", "draft"])
def test_approve_only_on_the_merged_head_this_window_read(
    window: Callable[[], Window],
    repo: Repo,
    github: GitHub,
    tmp_path: Path,
    verdict: Path,
    case: str,
) -> None:
    head = authored_pr(repo, tmp_path, {"code.py": "x = 1\n"})
    if case != "not-seen":
        review_checkout(repo)
    github.pr(head=head, decision="REVIEW_REQUIRED", draft=case == "draft")
    if case == "moved":
        github.update(head=authored_pr(repo, tmp_path, {"code.py": "x = 2\n"}))
    if case == "stale-main":
        repo.advance_main(20, "main ушёл вперёд")

    result = reviewer(window).run("approve", str(PR), head, str(verdict))

    assert result.returncode == 1
    expected = {
        "moved": "после твоего ревью в ветку пушили",
        "stale-main": "нет свежего main",
        "not-seen": "не из этой копии",
        "draft": "не в черновике",
    }[case]
    assert expected in result.stderr
    assert github.calls("api") == []


def test_foreign_pr_gets_no_verdict_and_no_place_in_the_queue(
    window: Callable[[], Window],
    pool_dir: Path,
    repo: Repo,
    github: GitHub,
    tmp_path: Path,
    verdict: Path,
) -> None:
    head = authored_pr(repo, tmp_path, {"code.py": "x = 1\n"})
    review_checkout(repo)
    github.pr(head=head, author="stranger")

    approve = reviewer(window).run("approve", str(PR), head, str(verdict))
    queued = enqueue(window, pool_dir)

    for result in (approve, queued):
        assert result.returncode == 1
        assert "посторонний" in result.stderr
    assert github.calls("api") == []
    assert queue_entries(pool_dir) == []
