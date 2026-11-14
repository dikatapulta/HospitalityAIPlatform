"""Заявку не взяли за срок — линия 10а алертера (issue #1050, часть 4; ERR-OPS-025).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``); метрики —
по настоящей выдаче ``shared/metrics``; проводка и контур — через ``run_alerter``
на ``httpx.MockTransport``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest

from hospitality.shared import metrics
from hospitality.shared.config import get_settings
from hospitality.tools.alerter import AlertMonitor, run_alerter
from hospitality.tools.alerter_unclaimed import (
    ERR_REQUESTS_UNCLAIMED,
    ForgottenRequests,
    read_forgotten_requests,
    unclaimed_line_enabled,
)
from tests.test_alerter import TENANT_A, TENANT_B, make_monitor, ready_probe

_RUNBOOK = "https://example.invalid/alerts.md"


def step(monitor: AlertMonitor, snapshot: dict[str, ForgottenRequests] | None) -> list[str]:
    return monitor.evaluate(ready_probe(ok=True, forgotten_requests=snapshot), now=0.0)


CLEAR = ForgottenRequests(urgent=0, regular=0, oldest_age_seconds=0.0)


# ---------------------------------------------------------------------------
# Парсер — по настоящей выдаче
# ---------------------------------------------------------------------------


def test_alerter_copy_of_the_snapshot_matches_the_application_one() -> None:
    """Алертер не импортирует код приложения: копия снимка обязана совпадать полями."""
    assert [field.name for field in dataclasses.fields(ForgottenRequests)] == [
        field.name for field in dataclasses.fields(metrics.ForgottenRequests)
    ]


def test_parser_reads_what_the_application_publishes() -> None:
    metrics.set_forgotten_requests(
        {
            TENANT_A: metrics.ForgottenRequests(urgent=1, regular=2, oldest_age_seconds=1800.0),
            TENANT_B: metrics.ForgottenRequests(urgent=0, regular=0, oldest_age_seconds=0.0),
        }
    )
    try:
        parsed = read_forgotten_requests(generate_latest(REGISTRY).decode())
    finally:
        metrics.set_forgotten_requests({})

    assert parsed == {
        TENANT_A: ForgottenRequests(urgent=1, regular=2, oldest_age_seconds=1800.0),
        TENANT_B: CLEAR,
    }


def test_cleared_snapshot_reads_as_empty() -> None:
    metrics.set_forgotten_requests({})

    assert read_forgotten_requests(generate_latest(REGISTRY).decode()) == {}


# ---------------------------------------------------------------------------
# Машина
# ---------------------------------------------------------------------------


def test_only_the_pilot_contour_has_the_line() -> None:
    assert unclaimed_line_enabled("pilot")
    assert not unclaimed_line_enabled("staging")
    assert not unclaimed_line_enabled("dev")


def test_staging_stays_silent_about_demo_requests() -> None:
    monitor = make_monitor(environment="staging")

    assert step(monitor, {TENANT_A: ForgottenRequests(0, 3, 7200.0)}) == []


def test_alert_once_then_recovery() -> None:
    monitor = make_monitor(environment="pilot")

    opened = step(monitor, {TENANT_A: ForgottenRequests(1, 1, 34 * 60.0)})
    again = step(monitor, {TENANT_A: ForgottenRequests(1, 2, 40 * 60.0)})
    recovered = step(monitor, {TENANT_A: CLEAR})

    assert len(opened) == 1
    assert ERR_REQUESTS_UNCLAIMED in opened[0] and "🔴" in opened[0]
    assert "ждут дольше срока: 2 (срочных и от бота: 1)" in opened[0]
    assert "самая старая ждёт 34 мин" in opened[0]
    assert f"tenant: {TENANT_A}" in opened[0]
    assert again == []
    assert len(recovered) == 1 and "✅" in recovered[0]
    assert "заявок дольше срока больше нет" in recovered[0]


def test_no_urgent_part_without_urgent_requests() -> None:
    monitor = make_monitor(environment="pilot")

    opened = step(monitor, {TENANT_A: ForgottenRequests(0, 1, 95 * 60.0)})

    assert "ждут дольше срока: 1\n" in opened[0]
    assert "самая старая ждёт 1 ч" in opened[0]


def test_unknown_snapshot_keeps_the_alert_open() -> None:
    """Пустой снимок (БД) — «не знаю»: ни ✅, ни нового алерта."""
    monitor = make_monitor(environment="pilot")
    step(monitor, {TENANT_A: ForgottenRequests(0, 1, 3600.0)})

    assert step(monitor, {}) == []
    assert step(monitor, None) == []
    assert [alert.error_code for alert in monitor.active_state_alerts()] == [ERR_REQUESTS_UNCLAIMED]


def test_tenants_are_separate() -> None:
    monitor = make_monitor(environment="pilot")
    step(monitor, {TENANT_A: ForgottenRequests(0, 1, 3600.0), TENANT_B: CLEAR})

    other = step(
        monitor,
        {TENANT_A: ForgottenRequests(0, 1, 3700.0), TENANT_B: ForgottenRequests(1, 0, 700.0)},
    )

    assert len(other) == 1 and f"tenant: {TENANT_B}" in other[0]


# ---------------------------------------------------------------------------
# Проводка /metrics → run_alerter и метка контура
# ---------------------------------------------------------------------------


@pytest.fixture
def alerter_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _forgotten_metrics() -> str:
    labels = f'tenant_id="{TENANT_A}"'
    return (
        f'service_requests_forgotten{{kind="urgent",{labels}}} 1.0\n'
        f'service_requests_forgotten{{kind="regular",{labels}}} 0.0\n'
        f"service_requests_forgotten_oldest_age_seconds{{{labels}}} 720.0\n"
    )


def _run(environment: str, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    monkeypatch.setenv("SENTRY_ENVIRONMENT", environment)
    get_settings.cache_clear()
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=_forgotten_metrics())
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"неожиданный запрос алертера: {request.url}")

    run_alerter(iterations=1, transport=httpx.MockTransport(handler))
    return sent


def test_run_alerter_reports_forgotten_request_on_pilot(
    alerter_env: pytest.MonkeyPatch,
) -> None:
    sent = _run("pilot", alerter_env)

    assert len(sent) == 1, sent
    assert "[pilot] ERR-OPS-025" in sent[0]["text"]
    assert "самая старая ждёт 12 мин" in sent[0]["text"]


def test_run_alerter_has_no_line_on_staging(alerter_env: pytest.MonkeyPatch) -> None:
    assert _run("staging", alerter_env) == []
