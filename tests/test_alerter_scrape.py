"""Алертер не видит ``/metrics`` — линия алертера (issue #264, ERR-OPS-007).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``); признак
«``/metrics`` не ответил» в ``probe_application`` и тракт до Telegram — на
httpx.MockTransport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from hospitality.shared.config import get_settings
from hospitality.tools.alerter import (
    ERR_READY_UNAVAILABLE,
    ERR_WORKER_STALLED,
    ProbeResult,
    probe_application,
    run_alerter,
)
from hospitality.tools.alerter_delivery import SendOutcome
from hospitality.tools.alerter_scrape import ERR_METRICS_UNAVAILABLE
from tests.test_alerter import (
    SAMPLE_METRICS,
    STALLED_WORKER_METRICS,
    FakeStagingStack,
    make_monitor,
    ready_probe,
)


def blind(*, ready_ok: bool = True) -> ProbeResult:
    """/metrics не ответил: все поля метрик — «не знаю», как в probe_application."""
    return ready_probe(
        ok=ready_ok,
        metrics_ok=False,
        metrics_detail="HTTP 502",
        errors_total=None,
        login_rate_limited_total=None,
    )


def seeing() -> ProbeResult:
    return ready_probe(ok=True)


def test_alerts_once_after_threshold_and_recovers() -> None:
    """Порог — общий с ERR-OPS-001 (в make_monitor — 2): одиночный чих не алерт."""
    monitor = make_monitor()

    first = monitor.evaluate(blind(), now=0.0)
    second = monitor.evaluate(blind(), now=60.0)
    third = monitor.evaluate(blind(), now=120.0)
    recovery = monitor.evaluate(seeing(), now=180.0)
    calm = monitor.evaluate(seeing(), now=240.0)

    assert first == []
    assert len(second) == 1 and ERR_METRICS_UNAVAILABLE in second[0] and "🔴" in second[0]
    assert "не видит /metrics 2 опроса(ов) подряд" in second[0]
    assert "HTTP 502; /health/ready при этом отвечает" in second[0]
    assert "мёртвый воркер" in second[0] and "#err-ops-007" in second[0]
    assert third == []  # алерт уже активен — не лента каждую минуту
    assert len(recovery) == 1 and "✅" in recovery[0] and ERR_METRICS_UNAVAILABLE in recovery[0]
    assert calm == []


def test_success_between_failures_resets_the_count() -> None:
    monitor = make_monitor()

    for now in (0.0, 120.0, 240.0):
        assert monitor.evaluate(blind(), now=now) == []
        assert monitor.evaluate(seeing(), now=now + 60.0) == []


def test_whole_application_down_is_err_ops_001_only() -> None:
    """Не ответили оба эндпоинта — лежит всё приложение: второго алерта о том же нет."""
    monitor = make_monitor()

    messages = [
        m for now in (0.0, 60.0, 120.0) for m in monitor.evaluate(blind(ready_ok=False), now=now)
    ]
    recovery = monitor.evaluate(seeing(), now=180.0)

    assert len(messages) == 1 and ERR_READY_UNAVAILABLE in messages[0]
    assert len(recovery) == 1 and ERR_READY_UNAVAILABLE in recovery[0]


def test_count_holds_while_application_is_down() -> None:
    """Опрос, где лежит всё приложение, счёт не сбрасывает и не растит: поднялось
    приложение без /metrics — алерт на следующем же опросе, а не через порог заново."""
    monitor = make_monitor()

    assert monitor.evaluate(blind(), now=0.0) == []
    down = monitor.evaluate(blind(ready_ok=False), now=60.0)
    back_without_metrics = monitor.evaluate(blind(), now=120.0)

    assert all(ERR_METRICS_UNAVAILABLE not in message for message in down)
    assert [ERR_METRICS_UNAVAILABLE in message for message in back_without_metrics] == [True]


def test_active_alert_survives_full_outage_and_recovers_once() -> None:
    monitor = make_monitor()
    monitor.evaluate(blind(), now=0.0)
    assert len(monitor.evaluate(blind(), now=60.0)) == 1

    down = [m for now in (120.0, 180.0) for m in monitor.evaluate(blind(ready_ok=False), now=now)]
    recovery = monitor.evaluate(seeing(), now=240.0)

    assert all(ERR_METRICS_UNAVAILABLE not in message for message in down)
    assert all("✅" in message for message in recovery)
    assert [ERR_METRICS_UNAVAILABLE in message for message in recovery] == [False, True]


def test_failed_send_postpones_the_alert() -> None:
    """Issue #288 и для этой линии: недоставленный алерт уходит следующим опросом."""
    monitor = make_monitor()
    attempts: list[str] = []
    outcomes = [SendOutcome.RETRY]

    def send(text: str) -> SendOutcome:
        attempts.append(text)
        return outcomes.pop(0) if outcomes else SendOutcome.DELIVERED

    for now in (0.0, 60.0, 120.0, 180.0):
        monitor.deliver(blind(), now=now, send=send)

    assert len(attempts) == 2 and all(ERR_METRICS_UNAVAILABLE in text for text in attempts)


# ---------------------------------------------------------------------------
# probe_application: «/metrics не ответил» отдельно от «метрика пуста»
# ---------------------------------------------------------------------------


def _probe(metrics: Callable[[], httpx.Response]) -> ProbeResult:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        return metrics()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return probe_application(client, "http://app:8000", backup=None, disk=None, offsite=None)


def test_probe_marks_non_200_metrics() -> None:
    probe = _probe(lambda: httpx.Response(503, text="Service Unavailable"))

    assert probe.ready_ok and not probe.metrics_ok
    assert probe.metrics_detail == "HTTP 503"
    assert probe.worker_heartbeat_age_seconds is None


def test_probe_marks_network_error_on_metrics() -> None:
    def refuse() -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    probe = _probe(refuse)

    assert not probe.metrics_ok and probe.metrics_detail == "connection error: timed out"


def test_probe_with_empty_metric_still_sees_metrics() -> None:
    """Пустая метрика (БД недоступна, NaN) — «не знаю» у линии воркера, но /metrics виден."""
    probe = _probe(lambda: httpx.Response(200, text="worker_heartbeat_age_seconds NaN\n"))

    assert probe.metrics_ok and probe.metrics_detail == ""
    assert probe.worker_heartbeat_age_seconds is None


# ---------------------------------------------------------------------------
# Цикл run_alerter: сценарий issue — ready жив, /metrics нет, воркер мёртв
# ---------------------------------------------------------------------------


@pytest.fixture
def alerter_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    monkeypatch.setenv("ALERT_READY_FAILURE_THRESHOLD", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_run_alerter_reports_blind_watchdog_then_dead_worker(alerter_settings: None) -> None:
    """До #264 первые два опроса были тишиной: воркер мёртв, а алертер не говорил ничего."""
    stack = FakeStagingStack(ready_statuses=[200, 200, 200, 200])
    answers = [
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, text=STALLED_WORKER_METRICS),
        httpx.Response(200, text=SAMPLE_METRICS),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return answers.pop(0)
        return stack.handler(request)

    run_alerter(iterations=4, transport=httpx.MockTransport(handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert ERR_METRICS_UNAVAILABLE in texts[0] and "HTTP 502" in texts[0]
    assert "✅" in texts[1] and ERR_METRICS_UNAVAILABLE in texts[1]
    assert any(ERR_WORKER_STALLED in text and "🔴" in text for text in texts[2:])
