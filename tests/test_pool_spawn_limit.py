"""Лимит одной модели у сторожа пула (#1109): стоят только окна этой модели.

12.11 окно Fable упёрлось в «You've reached your Fable limit», сторож этих слов не узнал,
трижды счёл окно оборвавшимся и остановил на час весь пул — и окна Opus, у которых лимит
не кончился. Теперь лимит модели держит только её окна (`.hold-<kind>`), общий лимит
подписки — по-прежнему весь пул (tests/test_pool_spawn.py). Пул и фейки — там же.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_pool_spawn import QUEUE_ITEMS, Pool, wait_for


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[Pool]:
    p = Pool(tmp_path)
    p.queue(f"{QUEUE_ITEMS}\n- **#4** — аудит — **Fable 5 `xhigh`**")
    p.issues(1, 4)
    yield p
    p.cleanup()


@pytest.mark.parametrize(("kind", "other"), [("fable", "opus"), ("opus", "fable")])
def test_model_limit_holds_only_windows_of_that_model(pool: Pool, kind: str, other: str) -> None:
    if kind == "fable":  # иначе первым пойдёт окно Opus: Fable — не чаще раза в час
        (pool.workers / ".fable-last").unlink()
    pool.mode(f"limit:{kind.capitalize()}")
    pool.tick()
    (limited,) = pool.ids()
    assert pool.field(limited, "kind") == kind
    pool.wait_exit(limited)
    pool.mode("sleep")
    (pool.workers / ".fable-last").unlink(missing_ok=True)

    out = pool.tick()

    assert f"лимит модели — окна {kind} стоят до" in out
    assert "запущено" in out, "окна другой модели идут"
    assert sorted(pool.field(w, "kind") for w in pool.ids()) == sorted([kind, other])
    assert pool.field(limited, "want") == f"лимит модели {kind}"
    status = pool.dispatcher.run("status").stdout
    assert f"окна {kind} стоят до" in status
    assert "сторож: стоит до" not in status
    pool.tick()
    assert not any("--resume" in args for args in pool.wait_calls(2)), "окно лимита ждёт"

    assert pool.dispatcher.run("resume").returncode == 0
    pool.tick()
    wait_for(lambda: any("--resume" in a for a in pool.calls()), dump=pool.dump)
    resumed = [a for a in pool.calls() if "--resume" in a]
    assert [a[a.index("--resume") + 1] for a in resumed] == [pool.field(limited, "session")]
    assert pool.field(limited, "tries") == "0", "лимит — не обрыв: попытка не тратится"


def test_opus_limit_keeps_new_opus_windows_waiting(pool: Pool) -> None:
    pool.mode("limit:Opus")
    pool.tick()
    (limited,) = pool.ids()
    pool.wait_exit(limited)

    out = pool.tick()

    assert "окна opus стоят до" in out
    assert pool.ids() == [limited], "Fable не настал, а новое окно Opus ждёт"
    assert len(pool.calls()) == 1
