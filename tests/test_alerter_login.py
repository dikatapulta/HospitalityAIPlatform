"""Вход персонала — линии алертера: упор в лимит (issue #486, ERR-OPS-016) и подбор
пароля ниже лимита (issue #967, ERR-OPS-020).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``);
проводка ``/metrics`` через ``run_alerter`` — там же, рядом с остальными линиями.
"""

from __future__ import annotations

from collections.abc import Callable

from prometheus_client import generate_latest

from hospitality.shared.metrics import record_staff_login
from hospitality.tools.alerter_login import (
    ERR_STAFF_LOGIN_GUESSING,
    ERR_STAFF_LOGIN_LOCKOUT,
    GUESSING_WINDOW_SECONDS,
    read_login_rate_limited,
    read_login_rejected,
)
from tests.test_alerter import make_monitor, ready_probe


def test_reads_rate_limited_from_real_exposition() -> None:
    """Парсер читает настоящую выдачу prometheus_client, а не придуманную строку."""
    before = read_login_rate_limited(generate_latest().decode())
    record_staff_login("rate_limited")
    record_staff_login("rejected")

    assert read_login_rate_limited(generate_latest().decode()) == before + 1


def test_reads_rejected_from_real_exposition() -> None:
    before = read_login_rejected(generate_latest().decode())
    record_staff_login("rejected")
    record_staff_login("rate_limited")

    assert read_login_rejected(generate_latest().decode()) == before + 1


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


def _guessing(threshold: int) -> Callable[[float | None, float], list[str]]:
    monitor = make_monitor(login_rejected_hourly_threshold=threshold)

    def step(total: float | None, now: float) -> list[str]:
        return monitor.evaluate(ready_probe(ok=True, login_rejected_total=total), now=now)

    return step


def test_slow_guessing_alerts_within_the_hour() -> None:
    """Подбор под лимитом (#967): по попытке в минуту — ни один опрос не всплеск,
    а за час набирается порог. Первый опрос — базовая линия: старые отказы не в счёт."""
    step = _guessing(threshold=30)
    assert step(500.0, 0.0) == []
    alerts = [message for minute in range(1, 31) for message in step(500.0 + minute, minute * 60.0)]

    assert len(alerts) == 1 and ERR_STAFF_LOGIN_GUESSING in alerts[0] and "🔴" in alerts[0]
    assert "неверных логинов или паролей: 30 (порог 30)" in alerts[0]
    assert "#err-ops-020" in alerts[0]


def test_old_rejections_leave_the_window() -> None:
    """Опечатки смены, размазанные по дню, порога не набирают: окно — последний час."""
    step = _guessing(threshold=30)
    alerts = [
        message
        for poll in range(0, 24 * 60, 5)  # 10 отказов в час, опрос каждые 5 минут
        for message in step(poll / 6.0, poll * 60.0)
    ]
    assert alerts == []


def test_guessing_repeats_once_per_window() -> None:
    step = _guessing(threshold=5)
    step(0.0, 0.0)
    first = step(10.0, 60.0)
    assert step(20.0, 120.0) == []  # тот же час — молчание
    again = step(30.0, 60.0 + GUESSING_WINDOW_SECONDS)

    assert len(first) == 1 and len(again) == 1


def test_guessing_unknown_and_restart_and_off() -> None:
    step = _guessing(threshold=5)
    step(40.0, 0.0)
    assert step(None, 60.0) == []  # /metrics не ответил — «не знаю»
    assert step(2.0, 120.0) == []  # перезапуск: счёт с нуля, 2 < 5
    restarted = step(6.0, 180.0)
    assert len(restarted) == 1 and ERR_STAFF_LOGIN_GUESSING in restarted[0]

    off = _guessing(threshold=0)
    off(0.0, 0.0)
    assert off(1000.0, 60.0) == []
