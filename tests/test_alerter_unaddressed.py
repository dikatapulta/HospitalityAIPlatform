"""ЧП и эскалация без адресата — линия 5д алертера (issue #1050, часть 4; ERR-OPS-024).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``); имя
метрики и метки — по настоящей выдаче ``shared/metrics``; проводка ``/metrics`` →
``run_alerter`` — на ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest

from hospitality.ai.escalation import EscalationReason
from hospitality.shared.config import get_settings
from hospitality.shared.metrics import record_escalation_unaddressed
from hospitality.tools.alerter import AlertMonitor, run_alerter
from hospitality.tools.alerter_escalation import FailedEscalations
from hospitality.tools.alerter_unaddressed import (
    ERR_ESCALATION_UNADDRESSED,
    REASON_LABELS,
    SeriesKey,
    read_unaddressed_escalations,
)
from tests.test_alerter import TENANT_A, TENANT_B, make_monitor, ready_probe

_COOLDOWN = 900.0  # make_monitor: cooldown_seconds
_STARTED = 1_793_000_000.0


def step(
    monitor: AlertMonitor,
    values: dict[SeriesKey, float] | None,
    *,
    now: float,
    created: float | None = _STARTED,
) -> list[str]:
    series = (
        None
        if values is None
        else {key: FailedEscalations(total, created) for key, total in values.items()}
    )
    return monitor.evaluate(ready_probe(ok=True, unaddressed_escalations=series), now=now)


def series_lines(tenant: str, reason: str, total: float) -> str:
    labels = f'reason="{reason}",tenant_id="{tenant}"'
    return (
        f"escalations_unaddressed_total{{{labels}}} {total}\n"
        f"escalations_unaddressed_created{{{labels}}} {_STARTED}"
    )


FIRE = (TENANT_A, "emergency")
LLM = (TENANT_A, "llm_unavailable")
ASKED = (TENANT_A, "guest_requested_staff")


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------


def test_reads_series_by_tenant_and_reason_with_birth() -> None:
    metrics = "\n".join(
        [
            "# TYPE escalations_unaddressed_total counter",
            series_lines(TENANT_A, "emergency", 2.0),
            series_lines(TENANT_B, "llm_unavailable", 5.0),
        ]
    )

    assert read_unaddressed_escalations(metrics) == {
        FIRE: FailedEscalations(total=2.0, created=_STARTED),
        (TENANT_B, "llm_unavailable"): FailedEscalations(total=5.0, created=_STARTED),
    }


def test_parser_reads_the_metric_the_application_writes() -> None:
    """Имя счётчика, метки и спутник — по настоящей выдаче: переименование выключило
    бы линию молча. Тенанта в контексте нет — `shared/metrics` пишет метку `none`."""
    key = ("none", "emergency")
    before = read_unaddressed_escalations(generate_latest(REGISTRY).decode()).get(key)

    record_escalation_unaddressed("emergency")

    after = read_unaddressed_escalations(generate_latest(REGISTRY).decode())[key]
    assert after.total == (before.total if before else 0.0) + 1
    assert after.created is not None


def test_every_escalation_reason_has_words_for_the_team() -> None:
    assert {reason.value for reason in EscalationReason} == REASON_LABELS.keys()


# ---------------------------------------------------------------------------
# Машина
# ---------------------------------------------------------------------------


def test_first_snapshot_is_baseline_then_emergency_alerts_at_once() -> None:
    monitor = make_monitor()

    assert step(monitor, {FIRE: 1.0}, now=0.0) == []  # прошлое — не новость
    alert = step(monitor, {FIRE: 2.0}, now=60.0)

    assert len(alert) == 1
    assert ERR_ESCALATION_UNADDRESSED in alert[0] and "🔴" in alert[0]
    assert "🚨 ЧП без адресата: 1" in alert[0]
    assert "Позвоните на ресепшен сами" in alert[0]
    assert f"tenant: {TENANT_A}" in alert[0]


def test_every_new_emergency_alerts_without_pause() -> None:
    """Второе ЧП через минуту — уже другой гость: пауза его не держит."""
    monitor = make_monitor()
    step(monitor, {}, now=0.0)

    first = step(monitor, {FIRE: 1.0}, now=60.0)
    second = step(monitor, {FIRE: 3.0}, now=120.0)

    assert len(first) == 1 and "ЧП без адресата: 1" in first[0]
    assert len(second) == 1 and "ЧП без адресата: 2" in second[0]


def test_other_reasons_pause_and_come_back_as_one_count_by_reason() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)

    first = step(monitor, {LLM: 1.0}, now=60.0)
    assert step(monitor, {LLM: 2.0, ASKED: 1.0}, now=120.0) == []  # пауза
    second = step(monitor, {LLM: 3.0, ASKED: 1.0}, now=60.0 + _COOLDOWN)
    assert step(monitor, {LLM: 3.0, ASKED: 1.0}, now=60.0 + 3 * _COOLDOWN) == []

    assert len(first) == 1 and "эскалаций без адресата: 1" in first[0]
    assert "ИИ не ответил — 1" in first[0] and "15 мин" in first[0]
    assert len(second) == 1 and "эскалаций без адресата: 3" in second[0]
    assert "ИИ не ответил — 2" in second[0] and "гость просит сотрудника — 1" in second[0]


def test_emergency_is_not_held_by_the_pause_of_other_reasons() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    step(monitor, {LLM: 1.0}, now=60.0)

    during_pause = step(monitor, {LLM: 2.0, FIRE: 1.0}, now=120.0)

    assert len(during_pause) == 1 and "ЧП без адресата: 1" in during_pause[0]


def test_restart_with_the_same_value_is_seen_by_series_birth() -> None:
    """Деплой: один случай до рестарта, один после — то же значение, другое рождение."""
    monitor = make_monitor()
    step(monitor, {FIRE: 1.0}, now=0.0)

    alert = step(monitor, {FIRE: 1.0}, now=60.0, created=_STARTED + 30)

    assert len(alert) == 1 and "ЧП без адресата: 1" in alert[0]


def test_unknown_snapshot_keeps_state() -> None:
    monitor = make_monitor()
    step(monitor, {FIRE: 1.0}, now=0.0)

    assert step(monitor, None, now=60.0) == []
    alert = step(monitor, {FIRE: 2.0}, now=120.0)

    assert len(alert) == 1


def test_tenants_are_separate() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)
    step(monitor, {LLM: 1.0}, now=60.0)

    other = step(monitor, {LLM: 1.0, (TENANT_B, "llm_unavailable"): 1.0}, now=120.0)

    assert len(other) == 1 and f"tenant: {TENANT_B}" in other[0]


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


def test_run_alerter_reports_unaddressed_emergency_from_metrics(alerter_env: None) -> None:
    """Снятая строка проводки в probe_application краснеет только здесь."""
    metrics = ["", series_lines(TENANT_A, "emergency", 1.0)]
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
    assert ERR_ESCALATION_UNADDRESSED in sent[0]["text"] and "ЧП без адресата" in sent[0]["text"]
