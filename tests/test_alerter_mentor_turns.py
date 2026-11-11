"""ИИ в кабинете отказывает сотрудникам — линия алертера (issue #1050, ERR-OPS-021).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``); имя
метрики — по настоящей выдаче ``shared/metrics``; проводка ``/metrics`` →
``run_alerter`` — на ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest

from hospitality.shared.config import get_settings
from hospitality.shared.metrics import record_mentor_turn_failure
from hospitality.tools.alerter import AlertMonitor, run_alerter
from hospitality.tools.alerter_mentor_turns import (
    ERR_MENTOR_TURN_FAILURES,
    WINDOW_SECONDS,
    FailureCount,
    Key,
    read_mentor_turn_failures,
)
from tests.test_alerter import TENANT_A, TENANT_B, make_monitor, ready_probe

_COOLDOWN = 900.0  # make_monitor: cooldown_seconds
_STARTED = 1_793_000_000.0  # время рождения серий одного процесса приложения

ASK_ERROR: Key = (TENANT_A, "ask", "exception")
ASK_PROVIDER: Key = (TENANT_A, "ask", "provider")
EXAM_UNGRADED: Key = (TENANT_A, "exam_check", "ungraded")


def step(
    monitor: AlertMonitor,
    values: dict[Key, float] | None,
    *,
    now: float,
    created: float | None = _STARTED,
) -> list[str]:
    failures = (
        None
        if values is None
        else {key: FailureCount(total, created) for key, total in values.items()}
    )
    return monitor.evaluate(ready_probe(ok=True, mentor_turn_failures=failures), now=now)


def series_lines(key: Key, total: float) -> str:
    tenant_id, flow, reason = key
    labels = f'flow="{flow}",reason="{reason}",tenant_id="{tenant_id}"'
    return (
        f"mentor_turn_failures_total{{{labels}}} {total}\n"
        f"mentor_turn_failures_created{{{labels}}} {_STARTED}"
    )


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------


def test_reads_series_with_flow_reason_and_birth() -> None:
    metrics = "\n".join(
        [
            "# TYPE mentor_turn_failures_total counter",
            series_lines(ASK_ERROR, 2.0),
            series_lines(EXAM_UNGRADED, 1.0),
        ]
    )

    assert read_mentor_turn_failures(metrics) == {
        ASK_ERROR: FailureCount(total=2.0, created=_STARTED),
        EXAM_UNGRADED: FailureCount(total=1.0, created=_STARTED),
    }


def test_parser_reads_the_metric_the_application_writes() -> None:
    """Имя счётчика, метки и спутник — по настоящей выдаче: переименованная метрика
    выключила бы линию молча. Тенанта в контексте нет — метка `none`."""
    key: Key = ("none", "quiz_check", "ungraded")
    before = read_mentor_turn_failures(generate_latest(REGISTRY).decode()).get(key)

    record_mentor_turn_failure("quiz_check", "ungraded")

    after = read_mentor_turn_failures(generate_latest(REGISTRY).decode())[key]
    assert after.total == (before.total if before else 0.0) + 1
    assert after.created is not None


# ---------------------------------------------------------------------------
# Машина
# ---------------------------------------------------------------------------


def test_first_snapshot_is_baseline() -> None:
    monitor = make_monitor()

    assert step(monitor, {ASK_ERROR: 5.0}, now=0.0) == []


def test_three_failures_in_ten_minutes_alert_with_causes() -> None:
    """Порог — по всем причинам вместе: исключение и провайдер «через раз»."""
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    assert step(monitor, {ASK_ERROR: 1.0}, now=60.0) == []
    assert step(monitor, {ASK_ERROR: 1.0, ASK_PROVIDER: 1.0}, now=120.0) == []

    [message] = step(monitor, {ASK_ERROR: 2.0, ASK_PROVIDER: 1.0}, now=300.0)

    assert ERR_MENTOR_TURN_FAILURES in message
    assert f"tenant: {TENANT_A}" in message
    assert "3 отказа" in message
    assert "«Спросить» — ошибка кода" in message and ": 2" in message
    assert "провайдер модели не ответил" in message


def test_failures_older_than_the_window_do_not_add_up() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    step(monitor, {ASK_ERROR: 2.0}, now=60.0)

    assert step(monitor, {ASK_ERROR: 3.0}, now=60.0 + WINDOW_SECONDS + 1) == []


def test_one_ungraded_answer_alerts_at_once() -> None:
    """Ответ экзамена не засчитан, а сотрудник об этом не знает — порога нет."""
    monitor = make_monitor()
    step(monitor, {}, now=0.0)

    [message] = step(monitor, {EXAM_UNGRADED: 1.0}, now=60.0)

    assert "проверка экзамена — оценка ответа не разобрана" in message


def test_failures_inside_cooldown_are_not_lost() -> None:
    """Отказ, перешедший порог в паузе, приходит после неё — даже когда окно ушло."""
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    assert len(step(monitor, {EXAM_UNGRADED: 1.0}, now=60.0)) == 1
    assert step(monitor, {EXAM_UNGRADED: 2.0}, now=120.0) == []

    [message] = step(monitor, {EXAM_UNGRADED: 2.0}, now=60.0 + _COOLDOWN)

    assert "1 отказ" in message


def test_restart_with_the_same_value_is_seen_by_series_birth() -> None:
    """Деплой: три отказа до рестарта и три после — тот же счёт, другая серия."""
    monitor = make_monitor()
    step(monitor, {ASK_ERROR: 3.0}, now=0.0)

    [message] = step(monitor, {ASK_ERROR: 3.0}, now=60.0, created=_STARTED + 100)

    assert "3 отказа" in message


def test_unknown_snapshot_keeps_state_and_threshold_zero_turns_line_off() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    assert step(monitor, None, now=60.0) == []
    assert len(step(monitor, {EXAM_UNGRADED: 1.0}, now=120.0)) == 1

    monitor.mentor_turns.threshold = 0
    assert step(monitor, {EXAM_UNGRADED: 5.0}, now=_COOLDOWN * 2) == []


def test_tenants_are_counted_apart() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    other: Key = (TENANT_B, "ask", "exception")

    assert step(monitor, {ASK_ERROR: 2.0, other: 2.0}, now=60.0) == []


# ---------------------------------------------------------------------------
# Проводка /metrics → run_alerter
# ---------------------------------------------------------------------------


@pytest.fixture
def alerter_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_run_alerter_reports_ungraded_exam_answer_from_metrics(alerter_env: None) -> None:
    """Тракт «HTTP-ответ → парсер → машина → отправка»: снятая строка проводки
    в probe_application или run_alerter краснеет только здесь."""
    metrics = ["", series_lines(EXAM_UNGRADED, 1.0)]
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=metrics.pop(0))
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"неожиданный запрос алертера: {request.url}")

    run_alerter(iterations=2, transport=httpx.MockTransport(handler))

    assert len(sent) == 1, sent
    assert ERR_MENTOR_TURN_FAILURES in sent[0]["text"]
    assert f"tenant: {TENANT_A}" in sent[0]["text"]
