"""Персонал упирается в лимит попыток входа — линия алертера (issue #486, ERR-OPS-016).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``);
проводка ``/metrics`` через ``run_alerter`` — там же, рядом с остальными линиями.
"""

from __future__ import annotations

from prometheus_client import generate_latest

from hospitality.shared.metrics import record_staff_login
from hospitality.tools.alerter_login import ERR_STAFF_LOGIN_LOCKOUT, read_login_rate_limited
from tests.test_alerter import make_monitor, ready_probe


def test_reads_rate_limited_from_real_exposition() -> None:
    """Парсер читает настоящую выдачу prometheus_client, а не придуманную строку."""
    before = read_login_rate_limited(generate_latest().decode())
    record_staff_login("rate_limited")
    record_staff_login("rejected")

    assert read_login_rate_limited(generate_latest().decode()) == before + 1


def test_missing_series_is_zero() -> None:
    """Серия появляется после первого отказа: до него /metrics её не содержит."""
    assert read_login_rate_limited('staff_logins_total{outcome="ok"} 4.0\n') == 0.0


def test_alerts_on_growth_and_respects_cooldown() -> None:
    monitor = make_monitor(login_rate_limited_threshold=1)

    def step(total: float | None, now: float) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, login_rate_limited_total=total), now=now)

    assert step(2.0, 0.0) == []  # первый опрос — базовая линия, старые отказы не в счёт
    assert step(2.0, 60.0) == []
    first = step(5.0, 120.0)
    assert step(9.0, 180.0) == []  # молчание после алерта
    assert step(None, 240.0) == []  # /metrics не ответил — «не знаю»
    again = step(10.0, 120.0 + 900.0)

    assert len(first) == 1 and ERR_STAFF_LOGIN_LOCKOUT in first[0] and "🔴" in first[0]
    assert "с прошлой проверки: 3." in first[0]
    assert "#err-ops-016" in first[0]
    assert len(again) == 1 and "с прошлой проверки: 1." in again[0]


def test_counter_reset_counts_from_zero() -> None:
    """Перезапуск приложения обнуляет счётчик — прирост считается с нуля."""
    monitor = make_monitor(login_rate_limited_threshold=2)
    monitor.evaluate(ready_probe(ok=True, login_rate_limited_total=40.0), now=0.0)

    assert monitor.evaluate(ready_probe(ok=True, login_rate_limited_total=1.0), now=60.0) == []
    reset = monitor.evaluate(ready_probe(ok=True, login_rate_limited_total=3.0), now=120.0)
    assert len(reset) == 1 and ERR_STAFF_LOGIN_LOCKOUT in reset[0]


def test_zero_threshold_turns_line_off() -> None:
    monitor = make_monitor(login_rate_limited_threshold=0)
    monitor.evaluate(ready_probe(ok=True, login_rate_limited_total=0.0), now=0.0)

    assert monitor.evaluate(ready_probe(ok=True, login_rate_limited_total=50.0), now=60.0) == []
