"""Место на диске сервера — седьмая линия алертера (issue #414, ERR-OPS-012).

Машина состояний — на снимках, собранных руками (помощники ``tests/test_alerter.py``);
проводка ``ALERT_DISK_PATH`` через ``run_alerter`` — там же, рядом с проводкой бэкапа.
"""

from __future__ import annotations

from pathlib import Path

from hospitality.tools.alerter_disk import ERR_DISK_LOW, DiskProbe, describe_disk, probe_disk
from tests.test_alerter import make_monitor, ready_probe

_GIB = 1024**3
# Диск в 100 ГБ: процент свободного — ровно число гигабайт, без дробей.
_TOTAL = 100 * _GIB


def disk(free_percent: int) -> DiskProbe:
    return DiskProbe(path="/backups", free_bytes=free_percent * _GIB, total_bytes=_TOTAL)


def test_low_disk_alerts_once_and_recovers_with_margin() -> None:
    """Вход — ниже 15%, выход — только с запасом над порогом, а не на самой границе."""
    monitor = make_monitor(disk_min_free_ratio=0.15)

    def step(free_percent: int) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, disk=disk(free_percent)), now=0.0)

    assert step(40) == []
    low = step(13)
    assert step(12) == []  # алерт уже активен — не лента каждую минуту
    assert step(16) == []  # выше порога, но в пределах запаса — ещё не ✅
    recovery = step(18)

    assert len(low) == 1 and ERR_DISK_LOW in low[0] and "🔴" in low[0]
    assert "(13%), порог 15%" in low[0] and "Postgres" in low[0]
    assert len(recovery) == 1 and ERR_DISK_LOW in recovery[0] and "✅" in recovery[0]
    assert "(18%), порог 15%" in recovery[0]


def test_disk_line_without_data_is_silent_and_keeps_state() -> None:
    """None — линия выключена или «не знаю»: ни алерта, ни ✅, состояние прежнее."""
    monitor = make_monitor()

    def step(probe: DiskProbe | None) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, disk=probe), now=0.0)

    assert step(None) == []
    assert len(step(disk(5))) == 1
    assert step(None) == []
    assert step(disk(5)) == []  # «не знаю» посередине не сбросило активный алерт


def test_describe_disk_reads_like_df() -> None:
    """Гигабайты — двоичные, как у `df -h`; процент округлён вниз, чтобы 14,9% не
    выглядели в алерте как «15%, порог 15%»."""
    probe = DiskProbe(path="/backups", free_bytes=int(4.8 * _GIB), total_bytes=38 * _GIB)

    assert describe_disk(probe, min_free_ratio=0.15) == (
        "свободно 4,8 ГБ из 38 ГБ (12%), порог 15%"
    )


def test_probe_disk_reads_real_filesystem(tmp_path: Path) -> None:
    probe = probe_disk(str(tmp_path))

    assert probe is not None
    assert probe.total_bytes > 0
    assert 0 <= probe.free_bytes <= probe.total_bytes


def test_probe_disk_is_off_for_empty_path_and_unknown_for_missing(tmp_path: Path) -> None:
    assert probe_disk("") is None
    assert probe_disk(str(tmp_path / "нет-такого")) is None
