"""Task 0018: алертер — машина состояний, парсер метрик, цикл (§10.8).

Машина состояний и парсер — чистые функции без сети; цикл гоняется на
httpx.MockTransport (ни одного реального запроса).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from hospitality.shared.config import get_settings
from hospitality.shared.metrics import record_http_request
from hospitality.tools.alerter import (
    ERR_ERROR_SPIKE,
    ERR_READY_UNAVAILABLE,
    AlertMonitor,
    ProbeResult,
    run_alerter,
    sum_server_errors,
)

# ---------------------------------------------------------------------------
# Парсер /metrics
# ---------------------------------------------------------------------------

SAMPLE_METRICS = """\
# HELP http_requests_total HTTP-запросы по маршрутам (RED, FOUNDATION §10.7)
# TYPE http_requests_total counter
http_requests_total{method="GET",route="/a",status="5xx"} 7.0
http_requests_total{method="GET",route="/a",status="2xx"} 3.0
http_requests_total{method="POST",route="/b",status="5xx"} 2.0
outbox_pending_events 4.0
"""


def test_sum_server_errors_counts_only_5xx() -> None:
    assert sum_server_errors(SAMPLE_METRICS) == 9.0


def test_sum_server_errors_understands_real_exposition_format() -> None:
    """Парсер и выдача prometheus_client не должны разойтись молча."""
    from prometheus_client import generate_latest

    before = sum_server_errors(generate_latest().decode())
    record_http_request(
        method="GET", route="/alerter-parser-test", status_code=500, duration_seconds=0.01
    )
    after = sum_server_errors(generate_latest().decode())

    assert after == before + 1


# ---------------------------------------------------------------------------
# Машина состояний
# ---------------------------------------------------------------------------


def make_monitor() -> AlertMonitor:
    return AlertMonitor(
        ready_failure_threshold=2,
        error_spike_threshold=5,
        cooldown_seconds=900.0,
        environment="test",
        runbook_url="https://example.invalid/alerts.md",
    )


def ready_probe(*, ok: bool, errors_total: float | None = 0.0) -> ProbeResult:
    return ProbeResult(
        ready_ok=ok,
        ready_detail='{"status": "unavailable"}' if not ok else '{"status": "ok"}',
        server_error_total=errors_total,
    )


def test_ready_alert_fires_once_after_threshold_and_recovers() -> None:
    monitor = make_monitor()

    assert monitor.evaluate(ready_probe(ok=False), now=0.0) == []  # одиночный чих — не алерт
    second = monitor.evaluate(ready_probe(ok=False), now=60.0)
    third = monitor.evaluate(ready_probe(ok=False), now=120.0)
    recovery = monitor.evaluate(ready_probe(ok=True), now=180.0)

    assert len(second) == 1 and ERR_READY_UNAVAILABLE in second[0]
    assert "runbook" in second[0] and "tenant: platform" in second[0]
    assert third == []  # алерт уже активен — не спамим каждый опрос
    assert len(recovery) == 1 and "✅" in recovery[0]
    # Новый цикл падения после восстановления снова приводит к алерту.
    monitor.evaluate(ready_probe(ok=False), now=240.0)
    assert monitor.evaluate(ready_probe(ok=False), now=300.0) != []


def test_error_spike_alert_respects_baseline_and_cooldown() -> None:
    monitor = make_monitor()

    assert (
        monitor.evaluate(ready_probe(ok=True, errors_total=100.0), now=0.0) == []
    )  # базовая линия
    spike = monitor.evaluate(ready_probe(ok=True, errors_total=110.0), now=60.0)
    during_cooldown = monitor.evaluate(ready_probe(ok=True, errors_total=200.0), now=120.0)
    after_cooldown = monitor.evaluate(ready_probe(ok=True, errors_total=300.0), now=1200.0)

    assert len(spike) == 1 and ERR_ERROR_SPIKE in spike[0]
    assert during_cooldown == []
    assert len(after_cooldown) == 1


def test_error_spike_survives_counter_reset() -> None:
    """Перезапуск приложения обнуляет счётчики — отрицательная дельта не алертит
    ложно, а накопленное с нуля считается новой дельтой."""
    monitor = make_monitor()

    monitor.evaluate(ready_probe(ok=True, errors_total=100.0), now=0.0)
    small_after_reset = monitor.evaluate(ready_probe(ok=True, errors_total=2.0), now=60.0)
    big_after_reset = monitor.evaluate(ready_probe(ok=True, errors_total=20.0), now=120.0)

    assert small_after_reset == []
    assert len(big_after_reset) == 1


def test_unavailable_metrics_do_not_alert_and_keep_baseline() -> None:
    monitor = make_monitor()

    monitor.evaluate(ready_probe(ok=True, errors_total=100.0), now=0.0)
    unavailable = monitor.evaluate(ready_probe(ok=False, errors_total=None), now=60.0)
    recovered = monitor.evaluate(ready_probe(ok=True, errors_total=103.0), now=120.0)

    assert unavailable == []  # /metrics упал вместе с приложением — покроет ERR-OPS-001
    assert recovered == []  # базовая линия не потеряна: дельта 3 < порога


# ---------------------------------------------------------------------------
# Цикл run_alerter (httpx.MockTransport, без сети и без сна)
# ---------------------------------------------------------------------------


@pytest.fixture
def alerter_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ALERT_READY_FAILURE_THRESHOLD", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeStagingStack:
    """Фейковые /health/ready, /metrics и Telegram Bot API в одном транспорте."""

    def __init__(self, ready_statuses: list[int]) -> None:
        self.ready_statuses = ready_statuses
        self.sent_messages: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            status = self.ready_statuses.pop(0)
            return httpx.Response(status, json={"status": "ok" if status == 200 else "unavailable"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=SAMPLE_METRICS)
        if request.url.path.endswith("/sendMessage"):
            self.sent_messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"неожиданный запрос алертера: {request.url}")


def test_run_alerter_sends_alert_and_recovery(alerter_settings: None) -> None:
    stack = FakeStagingStack(ready_statuses=[503, 503, 200])

    run_alerter(iterations=3, transport=httpx.MockTransport(stack.handler))

    assert len(stack.sent_messages) == 2
    alert, recovery = stack.sent_messages
    assert alert["chat_id"] == "-100777"
    assert ERR_READY_UNAVAILABLE in alert["text"] and "🔴" in alert["text"]
    assert "✅" in recovery["text"]


def test_run_alerter_survives_telegram_send_failure(alerter_settings: None) -> None:
    """Сбой отправки логируется, но не роняет цикл (следующая итерация живёт)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(503, json={"status": "unavailable"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=SAMPLE_METRICS)
        return httpx.Response(500, json={"ok": False})

    run_alerter(iterations=3, transport=httpx.MockTransport(handler))  # не бросает


def test_half_configured_pair_fails_fast(
    alerter_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    get_settings.cache_clear()

    with pytest.raises(SystemExit):
        run_alerter(iterations=1)


def test_unconfigured_alerter_is_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    get_settings.cache_clear()

    def forbid_sleep(_seconds: float) -> None:
        raise AssertionError("одна итерация пассивного цикла не должна спать")

    monkeypatch.setattr("hospitality.tools.alerter.time.sleep", forbid_sleep)
    try:
        run_alerter(iterations=1)  # не бросает и не ходит в сеть
    finally:
        get_settings.cache_clear()
