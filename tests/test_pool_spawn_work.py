"""Проверка «есть что брать» сторожа пула (#743): без модели, перед запуском окна.

Окно запускается, только если ему есть что брать по тем же пропускам, по которым окно
выбирает работу в турникете (docs/QUEUE.md, «Пул окон», шаг 2): PR без заявки, ждущий
ревью, доработки или merge и не стоящий в очереди слияния, или незанятый пункт раздела 1
без пометки «[ждёт основателя]»; окну Fable — только пункт Fable, окну многоагентного режима
(#959) — только пункт с пометкой `ultracode`. Сбой проверки — окно запускается, как до неё,
кроме многоагентного. Пул и фейки — tests/test_pool_spawn.py.
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
    head = "0" * 40
    approve_commented = make_pr(593) | {  # approve безголовому окну запрещён (#324)
        "headRefOid": head,
        "comments": [
            {
                "authorAssociation": "OWNER",
                "createdAt": "2026-11-11T00:00:00Z",
                "body": f"Вердикт комментарием: approve, голова {head}\n\nТекст ревью",
            }
        ],
    }
    prs = [
        make_pr(589, draft=True) | {"title": "Заявка (#2)"},
        make_pr(590, decision="APPROVED"),
        make_pr(591),
        approve_commented,
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


def test_pr_leaving_the_merge_queue_wakes_the_spawner(pool: Pool) -> None:
    """Воркер снял PR из очереди слияния с approve на месте (красный CI) — PR снова ждёт окна."""
    nothing_to_take(pool)
    assert "проверено без окна" in pool.tick()

    for entry in (pool.dir / "merge-queue").iterdir():
        entry.unlink()
    out = pool.tick()

    assert "запущено" in out
    assert len(pool.ids()) == 1


def test_idle_mark_keeps_the_snapshot_from_the_window_start(pool: Pool) -> None:
    """12.11: пока окно решало «брать нечего», #1108 вышел из очереди слияния без approve.

    Снимок, снятый при уходе окна, уже видел эту перемену — сторож счёл пул неизменным и час
    не запускал окно на ревью (#1109). Метке нужен снимок с запуска окна.
    """
    pool.mode("idle")
    approved = make_pr(590, decision="APPROVED")
    pool.prs(make_pr(589, draft=True), approved)
    queued = pool.dir / "merge-queue" / "20261112080000-590"
    queued.parent.mkdir()
    queued.write_text("590 head590\n")
    pool.tick()  # окну есть пункт #1 — запущено
    (first,) = pool.ids()
    pool.wait_exit(first)

    pool.issues()  # пункт #1 взят; #590 за жизнь окна: новый коммит снял approve и очередь
    pool.prs(make_pr(589, draft=True), approved | {"reviewDecision": None, "headRefOid": "new"})
    queued.unlink()
    out = pool.tick()

    assert f"окно {first}: закончило" in out
    assert "запущено" in out
    assert len(pool.ids()) == 1 and pool.ids() != [first]


def test_neighbour_windows_taking_and_leaving_numbers_do_not_wake_the_spawner(pool: Pool) -> None:
    """12.11 с 14:37 два пустых окна Opus перекрывались: номер соседа попадал в снимок метки
    «брать нечего», сосед уходил — снимок «менялся», и сторож раз в минуту запускал новое
    пустое окно (#1117). Работы от номеров окон не прибавляется — в снимке сторожа их нет.
    """
    pool.mode("idle")  # пункт #1 окна пропускают по его строке — окно уходит с «брать нечего»
    neighbour = pool.window()
    assert neighbour.run("slot").returncode == 0  # соседнее пустое окно держит номер
    pool.tick()
    (first,) = pool.ids()
    pool.wait_exit(first)

    assert neighbour.run("release").returncode == 0  # соседнее ушло, номер свободен
    out = pool.tick()

    assert f"окно {first}: закончило" in out
    assert "жду изменений в пуле" in out
    assert pool.ids() == []
    late = pool.window()
    assert late.run("slot").returncode == 0  # пришло новое окно — номер снова занят
    assert "запущено" not in pool.tick()
    assert pool.ids() == []


# Многоагентный режим (#959): пункт с пометкой `ultracode` вместо effort берёт только окно ultra.
ULTRA_ITEM = "- **#7** — аудит перед запуском — **{model} `ultracode`**"


@pytest.mark.parametrize(("mark", "model"), [("Fable 5", "fable"), ("Opus 5.5", "opus")])
def test_ultracode_item_gets_a_single_multiagent_window_on_its_model(
    pool: Pool, mark: str, model: str
) -> None:
    pool.queue(ULTRA_ITEM.format(model=mark))
    pool.issues(7)
    pool.mode("sleep")

    pool.tick()
    out = pool.tick()

    (worker,) = pool.ids()
    assert pool.field(worker, "kind") == "ultra"
    (args,) = pool.wait_calls(1)
    for flag, value in [
        ("--model", model),
        ("--effort", "ultracode"),
        ("--allowedTools", "Workflow"),
        ("--permission-mode", "auto"),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "многоагентном режиме" in args[-1]
    assert "брать нечего — проверено без окна" in out, "обычному окну пункт ultra — не работа"


def test_multiagent_window_resumes_after_usage_limit_in_the_same_mode(pool: Pool) -> None:
    pool.queue(ULTRA_ITEM.format(model="Fable 5"))
    pool.issues(7)
    pool.mode("limit")
    pool.tick()
    (worker,) = pool.ids()
    pool.wait_exit(worker)
    assert "лимит подписки" in pool.tick()
    pool.mode("sleep")
    assert pool.dispatcher.run("resume").returncode == 0

    pool.tick()

    args = pool.wait_calls(2)[-1]
    assert args[args.index("--resume") + 1] == pool.field(worker, "session")
    for flag, value in [
        ("--model", "fable"),
        ("--effort", "ultracode"),
        ("--allowedTools", "Workflow"),
    ]:
        assert args[args.index(flag) + 1] == value


def test_failed_check_never_starts_a_multiagent_window_blindly(pool: Pool) -> None:
    pool.queue(ULTRA_ITEM.format(model="Fable 5"))
    pool.issues(7)
    (pool.fake / "issues-down").write_text("")

    out = pool.tick()

    assert "проверка «есть что брать» не удалась — окно opus запускаю" in out
    assert [pool.field(w, "kind") for w in pool.ids()] == ["opus"]
