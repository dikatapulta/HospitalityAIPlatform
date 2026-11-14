"""Место в очереди слияния (ops/pool-merge.sh, ops/pool-verdict.sh) — #1057 п. 4.

`enqueue --front` — срочная починка: раньше всех записей. PR, снятый воркером открытым
(конфликт, красный check), после нового approve встаёт на прежнее место, а не в хвост.
Конфликт с main только в документах воркер отдаёт ревьюеру, а не автору: слова его
request changes ловит ops/queue.sh (состояние DOCS_CONFLICT, tests/test_queue_foreign.py).
Стенд — tests/test_pool_merge.py.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests import test_pool_merge as bench
from tests.test_pool_merge import (
    PR,
    GitHub,
    Repo,
    Window,
    enqueue,
    pretend_worker,
    queue_entries,
    run_worker,
)

env = bench.env
pool_dir = bench.pool_dir
repo = bench.repo
github = bench.github
window = bench.window

OLD = "20260101000000"


def test_front_entry_goes_before_older_ones(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "срочно")
    repo.set_pr_head(head)
    github.pr(head=head)
    (pool_dir / "merge-queue").mkdir(parents=True)
    (pool_dir / "merge-queue" / f"{OLD}-99").write_text("99 abc\n")
    pretend_worker(pool_dir, window())

    result = window().run("enqueue", "--front", str(PR))

    assert result.returncode == 0, result.stderr
    assert "первым — срочно" in result.stdout
    first, second = queue_entries(pool_dir)
    assert first.startswith("0") and first.endswith(f"-{PR}")
    assert second == f"{OLD}-99"


def test_pr_dropped_by_conflict_returns_to_its_place(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    approved = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(approved)
    github.pr(head=approved)
    enqueue(window, pool_dir)
    [entry] = queue_entries(pool_dir)
    (pool_dir / "merge-queue" / entry).rename(pool_dir / "merge-queue" / f"{OLD}-{PR}")
    repo.advance_main(3, "main")

    run_worker(window)

    assert queue_entries(pool_dir) == []
    assert (pool_dir / "merge-dropped" / str(PR)).read_text().strip() == f"{OLD}-{PR}"
    github.pr(head=approved)  # доработка и новый approve
    result = enqueue(window, pool_dir)
    assert "на прежнее место" in result.stdout
    assert queue_entries(pool_dir) == [f"{OLD}-{PR}"]
    assert not (pool_dir / "merge-dropped" / str(PR)).exists()


def test_merged_pr_leaves_no_place_behind(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    head = repo.edit_line(repo.main(), 3, "pr")
    repo.set_pr_head(head)
    github.pr(head=head)
    enqueue(window, pool_dir)

    run_worker(window)

    assert github.state()["state"] == "MERGED"
    assert not (pool_dir / "merge-dropped" / str(PR)).exists()


def conflict_in(repo: Repo, github: GitHub, files: dict[str, tuple[str, str]]) -> None:
    """main и PR правят одни и те же файлы по-разному: {файл: (в PR, в main)}."""
    base = repo.set_main(repo.change(repo.main(), {f: "общее\n" for f in files}))
    approved = repo.change(base, {f: pr for f, (pr, _) in files.items()})
    repo.set_pr_head(approved)
    github.pr(head=approved)
    repo.set_main(repo.change(base, {f: main for f, (_, main) in files.items()}))


def test_docs_only_conflict_is_left_to_the_reviewer(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    conflict_in(repo, github, {"NOTES.md": ("из PR\n", "из main\n")})
    enqueue(window, pool_dir)

    run_worker(window)

    [review] = github.calls("api")
    assert "event=REQUEST_CHANGES" in review["args"]
    [body] = [a for a in review["args"] if a.startswith("body=")]
    assert "конфликт с main только в документах (NOTES.md)" in body
    assert "окно-ревьюер" in body
    assert github.calls("pr", "update-branch") == []


def test_conflict_touching_code_still_goes_to_the_author(
    window: Callable[[], Window], pool_dir: Path, repo: Repo, github: GitHub
) -> None:
    conflict_in(repo, github, {"NOTES.md": ("a\n", "b\n"), "code.py": ("x = 1\n", "x = 2\n")})
    enqueue(window, pool_dir)

    run_worker(window)

    [review] = github.calls("api")
    [body] = [a for a in review["args"] if a.startswith("body=")]
    assert "нужна доработка: конфликт с main" in body
    assert "только в документах" not in body
