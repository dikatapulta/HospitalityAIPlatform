"""Копия бэкапа вне сервера устарела — линия алертера (issue #1061, ERR-OPS-023).

Машина состояний — на снимках, собранных руками (помощники ``tests/test_alerter.py``);
чтение метки — на настоящем каталоге; проводка через ``run_alerter`` — последним тестом.
Метку ставит ``ops/backup/fetch.sh`` — его сторона в ``tests/test_backup_fetch.py``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from hospitality.shared.config import get_settings
from hospitality.tools.alerter import run_alerter
from hospitality.tools.alerter_offsite import (
    ERR_OFFSITE_STALE,
    OFFSITE_STAMP_NAME,
    OffsiteProbe,
    probe_offsite_copy,
)
from tests.test_alerter import FakeStagingStack, make_monitor, ready_probe

_HOUR = 3600.0


def _stamp(directory: Path, *, age_hours: float) -> None:
    path = directory / OFFSITE_STAMP_NAME
    path.touch()
    moment = time.time() - age_hours * _HOUR
    os.utime(path, (moment, moment))


def test_stale_copy_alerts_once_and_recovers() -> None:
    """Вход — старше порога, одно сообщение, а не лента; ✅ — когда метка свежая."""
    monitor = make_monitor(offsite_max_age_hours=48.0)

    def step(age_hours: float) -> list[str]:
        probe = OffsiteProbe(age_seconds=age_hours * _HOUR)
        return monitor.evaluate(ready_probe(ok=True, offsite=probe), now=0.0)

    assert step(30) == []
    assert step(48) == []  # ровно на пороге — ещё не алерт
    stale = step(49)
    assert step(120) == []
    recovery = step(9)

    assert len(stale) == 1 and ERR_OFFSITE_STALE in stale[0] and "🔴" in stale[0]
    assert "49 ч (порог 48 ч)" in stale[0] and "fetch-agent.log" in stale[0]
    assert len(recovery) == 1 and ERR_OFFSITE_STALE in recovery[0] and "✅" in recovery[0]
    assert "9 ч" in recovery[0]


def test_missing_stamp_is_an_alert_not_silence() -> None:
    """Метки нет — копию вне сервера не забирали ни разу: это и есть отказ."""
    monitor = make_monitor(offsite_max_age_hours=48.0)

    messages = monitor.evaluate(
        ready_probe(ok=True, offsite=OffsiteProbe(age_seconds=None)), now=0.0
    )

    assert len(messages) == 1 and ERR_OFFSITE_STALE in messages[0]
    assert f"метки {OFFSITE_STAMP_NAME} в каталоге бэкапов нет" in messages[0]


def test_long_outage_reads_in_days() -> None:
    monitor = make_monitor(offsite_max_age_hours=48.0)
    probe = OffsiteProbe(age_seconds=5 * 24 * _HOUR)

    messages = monitor.evaluate(ready_probe(ok=True, offsite=probe), now=0.0)

    assert "5 сут (порог 48 ч)" in messages[0]


def test_zero_threshold_turns_the_line_off() -> None:
    """Умолчание 0 — staging: без метки и с древней меткой — тишина."""
    monitor = make_monitor()

    for probe in (OffsiteProbe(age_seconds=None), OffsiteProbe(age_seconds=1e9)):
        assert monitor.evaluate(ready_probe(ok=True, offsite=probe), now=0.0) == []


def test_unknown_keeps_state_and_stays_silent() -> None:
    """None — каталог не открывается: молчание и прежнее состояние (его зовёт ERR-OPS-008)."""
    monitor = make_monitor(offsite_max_age_hours=48.0)

    def step(probe: OffsiteProbe | None) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, offsite=probe), now=0.0)

    assert step(None) == []
    assert len(step(OffsiteProbe(age_seconds=None))) == 1
    assert step(None) == []
    assert step(OffsiteProbe(age_seconds=None)) == []  # «не знаю» не сбросило алерт


def test_probe_reads_stamp_age(tmp_path: Path) -> None:
    _stamp(tmp_path, age_hours=30)

    probe = probe_offsite_copy(str(tmp_path), now=time.time())

    assert probe is not None and probe.age_seconds is not None
    assert abs(probe.age_seconds - 30 * _HOUR) < 60


def test_probe_tells_missing_stamp_from_missing_directory(tmp_path: Path) -> None:
    assert probe_offsite_copy("", now=time.time()) is None
    assert probe_offsite_copy(str(tmp_path / "нет-такого"), now=time.time()) is None
    assert probe_offsite_copy(str(tmp_path), now=time.time()) == OffsiteProbe(age_seconds=None)


def test_probe_clamps_future_stamp_to_zero(tmp_path: Path) -> None:
    _stamp(tmp_path, age_hours=-2)

    assert probe_offsite_copy(str(tmp_path), now=time.time()) == OffsiteProbe(age_seconds=0.0)


@pytest.fixture
def alerter_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_run_alerter_reports_stale_stamp_from_disk(
    alerter_settings: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Тракт «метка на диске → машина → отправка»: единственное место, где покраснеет
    снятая проводка ``probe_offsite_copy`` или настройки порога в ``run_alerter``.
    Свежий ночной дамп рядом — ERR-OPS-008 молчит, алерт ровно один."""
    dump = tmp_path / "hospitality-20261112T031501Z.dump.age"
    dump.write_bytes(b"age-encryption.org/v1\n")
    _stamp(tmp_path, age_hours=60)
    monkeypatch.setenv("ALERT_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("ALERT_OFFSITE_BACKUP_MAX_AGE_HOURS", "48")
    get_settings.cache_clear()
    stack = FakeStagingStack(ready_statuses=[200])

    run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert len(texts) == 1, texts
    assert ERR_OFFSITE_STALE in texts[0] and "60 ч (порог 48 ч)" in texts[0]
