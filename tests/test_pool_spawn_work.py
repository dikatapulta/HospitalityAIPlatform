"""Проверка «есть что брать» сторожа пула (#743): без модели, перед запуском окна.

Окно запускается, только если ему есть что брать по тем же пропускам, по которым окно
выбирает работу в турникете (docs/QUEUE.md, «Пул окон», шаг 2): PR без заявки, ждущий
ревью, доработки или merge и не стоящий в очереди слияния, или незанятый пункт раздела 1
без пометки «[ждёт основателя]»; окну Fable — только пункт Fable. Сбой проверки — окно
запускается, как до неё. Пул и фейки — tests/test_pool_spawn.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_pool_spawn import QUEUE_ITEMS, Pool, Window, make_pr

NOTHING_QUEUE = "\n".join(
    [
        "- **#1** [ждёт основателя] — ждёт слова основателя — **Opus 5.5 `high`**",
        "- **#2** — занят черновиком — **Opus 5.5 `high`**",
        "- **#3** — issue закрыт — **Opus 5.5 `high`**",
        "- **#4** — аудит, модель в продолжении строки —",
        "  **Fable 5 `xhigh`**",
    ]
)


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[Pool]:
    p = Pool(tmp_path)
    yield p
    p.cleanup()


def nothing_to_take(pool: Pool) -> tuple[Window, list[dict[str, Any]]]:
    """Всё, что окно Opus прочло бы в турникете, взято или ждёт: брать нечего."""
    pool.queue(NOTHING_QUEUE)
    pool.issues(1, 2, 4)
    prs = [
        make_pr(589, draft=True) | {"title": "Заявка (#2)"},
        make_pr(590, decision="APPROVED"),
        make_pr(591),
    ]
    pool.prs(*prs)
    merge_queue = pool.dir / "merge-queue"
    merge_queue.mkdir()
    (merge_queue / "20261103080000-590").write_text("590 head590\n")
    reviewer = pool.window()
    for step in (("slot",), ("enter",), ("claim", "review-591"), ("leave",)):
        result = reviewer.run(*step)
        assert result.returncode == 0, result.stderr
    return reviewer, prs


def check_runs(pool: Pool) -> int:
    log = pool.fake / "gh.log"
    return log.read_text().count("issue list") if log.exists() else 0


def test_no_window_when_everything_is_taken_or_waits_for_founder(pool: Pool) -> None:
    nothing_to_take(pool)

    out = pool.tick()

    assert "брать нечего — проверено без окна" in out
    assert pool.ids() == []
    assert pool.calls() == []
    assert check_runs(pool) == 1
    out = pool.tick()
    assert "жду изменений в пуле" in out
    assert check_runs(pool) == 1, "пул не менялся — проверку не повторять"


@pytest.mark.parametrize(
    "work", ["item", "review", "pushed-rework", "rework", "merge", "abandoned-claim"]
)
def test_any_work_left_starts_a_window(pool: Pool, work: str) -> None:
    reviewer, prs = nothing_to_take(pool)
    if work == "item":
        pool.queue(f"{NOTHING_QUEUE}\n- **#5** — свободный пункт — **Opus 5.5 `high`**")
        pool.issues(1, 2, 4, 5)
    elif work == "abandoned-claim":
        reviewer.close()  # окно закрыто — его заявка review-591 брошена
    else:
        extra = {
            "review": make_pr(592),
            "pushed-rework": make_pr(592, decision="CHANGES_REQUESTED")
            | {
                "reviews": [
                    {"state": "CHANGES_REQUESTED", "submittedAt": "x", "commit": {"oid": "old"}}
                ]
            },
            "rework": make_pr(592, decision="CHANGES_REQUESTED"),
            "merge": make_pr(592, decision="APPROVED"),
        }[work]
        pool.prs(*prs, extra)

    out = pool.tick()

    assert "запущено" in out
    (worker,) = pool.ids()
    assert pool.field(worker, "kind") == "opus"


def test_fable_window_only_for_a_fable_item(pool: Pool) -> None:
    (pool.workers / ".fable-last").unlink()
    pool.mode("sleep")

    pool.tick()

    assert [pool.field(w, "kind") for w in pool.ids()] == ["opus"]
    pool.queue(f"{QUEUE_ITEMS}\n- **#4** — аудит — **Fable 5 `xhigh`**")
    pool.issues(1, 4)
    pool.prs(make_pr(589, draft=True), make_pr(600, draft=True))  # пул изменился
    pool.tick()
    assert sorted(pool.field(w, "kind") for w in pool.ids()) == ["fable", "opus"]


@pytest.mark.parametrize("failure", ["unparseable-queue", "issues-down", "no-origin-main"])
def test_failed_check_starts_a_window_as_before(pool: Pool, tmp_path: Path, failure: str) -> None:
    nothing_to_take(pool)
    extra: dict[str, str] = {}
    if failure == "unparseable-queue":
        pool.queue_file.write_text("# QUEUE.md без раздела 1\n", encoding="utf-8")
    elif failure == "issues-down":
        (pool.fake / "issues-down").write_text("")
    else:  # docs/QUEUE.md — из origin/main, а git его не отдаёт
        extra = {"POOL_QUEUE_FILE": "", "GIT_DIR": str(tmp_path / "no-git")}

    out = pool.tick(**extra)

    assert "проверка «есть что брать» не удалась" in out
    assert len(pool.ids()) == 1
