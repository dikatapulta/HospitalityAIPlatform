"""Карточка эскалации не завелась — линия 5б алертера (issue #434, ERR-OPS-015).

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
from hospitality.shared.metrics import record_escalation_request
from hospitality.tools.alerter import AlertMonitor, run_alerter
from hospitality.tools.alerter_escalation import (
    ERR_ESCALATION_CARD_FAILED,
    FailedEscalations,
    read_escalation_failures,
)
from tests.test_alerter import TENANT_A, TENANT_B, make_monitor, ready_probe

_COOLDOWN = 900.0  # make_monitor: cooldown_seconds
# Время рождения серии — один процесс приложения; другое значение — рестарт.
_STARTED = 1_793_000_000.0


def step(
    monitor: AlertMonitor,
    values: dict[str, float] | None,
    *,
    now: float,
    created: float | None = _STARTED,
) -> list[str]:
    failures = (
        None
        if values is None
        else {tenant: FailedEscalations(total, created) for tenant, total in values.items()}
    )
    return monitor.evaluate(ready_probe(ok=True, escalation_failures=failures), now=now)


def failed_lines(tenant: str, total: float) -> str:
    labels = f'outcome="failed",tenant_id="{tenant}"'
    return (
        f"escalation_requests_total{{{labels}}} {total}\n"
        f"escalation_requests_created{{{labels}}} {_STARTED}"
    )


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------


def test_reads_failed_outcome_and_series_birth_by_tenant() -> None:
    """Прочие исходы — не отказы; спутник `_created` — время рождения серии."""
    metrics = "\n".join(
        [
            "# TYPE escalation_requests_total counter",
            failed_lines(TENANT_A, 2.0),
            f'escalation_requests_total{{outcome="created",tenant_id="{TENANT_A}"}} 9.0',
            f'escalation_requests_total{{outcome="disabled",tenant_id="{TENANT_B}"}} 4.0',
            f'escalation_requests_created{{outcome="disabled",tenant_id="{TENANT_B}"}} 1.0',
        ]
    )

    assert read_escalation_failures(metrics) == {
        TENANT_A: FailedEscalations(total=2.0, created=_STARTED)
    }


def test_parser_reads_the_metric_the_application_writes() -> None:
    """Имя счётчика, метка исхода и спутник — по настоящей выдаче, а не по строке
    из головы: переименованная метрика выключила бы линию молча. Тенанта в
    контексте нет — `shared/metrics` пишет метку `none`."""
    before = read_escalation_failures(generate_latest(REGISTRY).decode()).get("none")

    record_escalation_request("failed")
    record_escalation_request("created")

    after = read_escalation_failures(generate_latest(REGISTRY).decode())["none"]
    assert after.total == (before.total if before else 0.0) + 1
    assert after.created is not None


# ---------------------------------------------------------------------------
# Машина состояний
# ---------------------------------------------------------------------------


def test_first_snapshot_is_baseline_then_each_new_failure_alerts() -> None:
    monitor = make_monitor()

    assert step(monitor, {TENANT_A: 3.0}, now=0.0) == []  # прошлое — не новость
    assert step(monitor, {TENANT_A: 3.0}, now=60.0) == []
    alert = step(monitor, {TENANT_A: 4.0}, now=120.0)

    assert len(alert) == 1
    assert ERR_ESCALATION_CARD_FAILED in alert[0] and "🔴" in alert[0]
    assert "эскалаций без карточки в кабинете: 1" in alert[0]
    assert f"tenant: {TENANT_A}" in alert[0]
    assert "escalation_request_skipped" in alert[0] and "15 мин" in alert[0]


def test_missing_series_is_zero_not_unknown() -> None:
    """Серия счётчика появляется только с первым отказом после старта приложения:
    отсутствие при ответившем /metrics — ноль, и первый отказ обязан алертить."""
    monitor = make_monitor()

    assert step(monitor, {}, now=0.0) == []
    alert = step(monitor, {TENANT_A: 1.0}, now=60.0)

    assert len(alert) == 1 and "без карточки в кабинете: 1" in alert[0]


def test_failures_inside_cooldown_are_counted_in_the_next_message() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)

    first = step(monitor, {TENANT_A: 1.0}, now=60.0)
    assert step(monitor, {TENANT_A: 3.0}, now=120.0) == []  # пауза
    assert step(monitor, {TENANT_A: 3.0}, now=60.0 + _COOLDOWN - 1) == []
    second = step(monitor, {TENANT_A: 3.0}, now=60.0 + _COOLDOWN)
    assert step(monitor, {TENANT_A: 3.0}, now=60.0 + 3 * _COOLDOWN) == []  # новых нет

    assert len(first) == 1 and "без карточки в кабинете: 1" in first[0]
    assert len(second) == 1 and "без карточки в кабинете: 2" in second[0]


def test_counter_reset_counts_everything_since_zero() -> None:
    """Рестарт приложения обнуляет счётчик: меньше прошлого — новое с нуля."""
    monitor = make_monitor()
    step(monitor, {TENANT_A: 5.0}, now=0.0)

    assert step(monitor, {}, now=60.0) == []  # рестарт, отказов ещё нет
    after_restart = step(monitor, {TENANT_A: 2.0}, now=120.0, created=_STARTED + 60)

    assert len(after_restart) == 1 and "без карточки в кабинете: 2" in after_restart[0]


def test_restart_with_the_same_value_is_seen_by_series_birth() -> None:
    """Откат образа — главный повод отказа: один отказ до рестарта, один после, и
    значение то же. Новую серию выдаёт только время её рождения."""
    monitor = make_monitor()
    step(monitor, {TENANT_A: 1.0}, now=0.0)

    alert = step(monitor, {TENANT_A: 1.0}, now=60.0, created=_STARTED + 600)

    assert len(alert) == 1 and "без карточки в кабинете: 1" in alert[0]


def test_without_series_birth_falls_back_to_value_drop() -> None:
    monitor = make_monitor()
    step(monitor, {TENANT_A: 3.0}, now=0.0, created=None)

    assert step(monitor, {TENANT_A: 3.0}, now=60.0, created=None) == []
    dropped = step(monitor, {TENANT_A: 1.0}, now=120.0, created=None)

    assert len(dropped) == 1 and "без карточки в кабинете: 1" in dropped[0]


def test_unknown_snapshot_keeps_state_and_is_not_a_baseline() -> None:
    """/metrics не ответил (None) — ни алерта, ни сдвига базовой линии."""
    monitor = make_monitor()

    assert step(monitor, None, now=0.0) == []
    assert step(monitor, {TENANT_A: 5.0}, now=60.0) == []  # базовая линия — этот снимок
    assert step(monitor, None, now=120.0) == []
    alert = step(monitor, {TENANT_A: 6.0}, now=180.0)

    assert len(alert) == 1 and "без карточки в кабинете: 1" in alert[0]


def test_cooldown_is_per_tenant() -> None:
    monitor = make_monitor()
    step(monitor, {}, now=0.0)

    first = step(monitor, {TENANT_A: 1.0}, now=60.0)
    second = step(monitor, {TENANT_A: 2.0, TENANT_B: 1.0}, now=120.0)

    assert len(first) == 1 and f"tenant: {TENANT_A}" in first[0]
    assert len(second) == 1 and f"tenant: {TENANT_B}" in second[0]


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


def test_run_alerter_reports_failed_escalation_card_from_metrics(alerter_env: None) -> None:
    """Тракт «HTTP-ответ → парсер → машина → отправка»: тесты машины собирают
    снимок руками, и снятая строка проводки в probe_application краснеет только
    здесь. Первый опрос — базовая линия без серии, второй — первый отказ."""
    metrics = ["", failed_lines(TENANT_A, 1.0)]
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
    assert ERR_ESCALATION_CARD_FAILED in sent[0]["text"]
    assert f"tenant: {TENANT_A}" in sent[0]["text"]
