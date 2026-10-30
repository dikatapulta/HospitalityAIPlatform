"""Issue #229: пульс алертера наружу и ERROR-логи алертера без секретов.

Всё на httpx.MockTransport — ни одного реального запроса.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, MutableMapping
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from hospitality.shared.config import get_settings
from hospitality.tools.alerter import run_alerter
from hospitality.tools.alerter_heartbeat import ERR_HEARTBEAT_FAILED, HeartbeatPinger

# Секретная часть адреса проверки: её не должно быть ни в одном логе.
CHECK_UUID = "5f1c9a2e-0000-4000-8000-0000deadbeef"
HEARTBEAT_URL = f"https://hc-ping.com/{CHECK_UUID}"
ALERT_TOKEN = "123456:alert-secret-token"


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _respond(status: int, text: str) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return handler


def _events(logs: list[MutableMapping[str, Any]], name: str) -> list[MutableMapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


# ---------------------------------------------------------------------------
# HeartbeatPinger
# ---------------------------------------------------------------------------


def test_empty_url_sends_nothing() -> None:
    def forbid(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("без адреса пульса запросов быть не должно")

    HeartbeatPinger(url="").maybe_ping(_client(forbid), now=0.0)


def test_recorded_ping_logs_nothing() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="OK")

    with capture_logs() as logs:
        HeartbeatPinger(url=HEARTBEAT_URL).maybe_ping(_client(handler), now=0.0)

    assert [str(request.url) for request in requests] == [HEARTBEAT_URL]
    assert logs == []


def test_pings_at_most_once_per_minute() -> None:
    """healthchecks.io не записывает больше 5 пульсов в минуту: шаг опроса
    короче минуты не должен превращаться в поток пульсов."""
    count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, text="OK")

    pinger = HeartbeatPinger(url=HEARTBEAT_URL)
    client = _client(handler)
    for now in (0.0, 10.0, 59.0, 60.0, 61.0, 120.5):
        pinger.maybe_ping(client, now=now)

    assert count == 3  # 0, 60, 120.5


def test_failure_is_one_error_per_outage_and_recovery_is_info() -> None:
    statuses = [500, 500, 500, 200]

    def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        return httpx.Response(status, text="OK" if status == 200 else "boom")

    pinger = HeartbeatPinger(url=HEARTBEAT_URL, min_interval_seconds=0)
    client = _client(handler)
    with capture_logs() as logs:
        for now in range(4):
            pinger.maybe_ping(client, now=float(now))

    failed = _events(logs, "alerter_heartbeat_failed")
    assert len(failed) == 1
    assert failed[0]["log_level"] == "error"
    assert failed[0]["error_code"] == ERR_HEARTBEAT_FAILED
    assert failed[0]["reason"] == "HTTP 500"
    assert len(_events(logs, "alerter_heartbeat_recovered")) == 1
    assert pinger.failing is False


@pytest.mark.parametrize("body", ["OK (not found)", "OK (rate limited)"])
def test_unrecorded_ping_with_status_200_is_a_failure(body: str) -> None:
    """Неизвестный адрес healthchecks.io отвечает 200, а не 404: проверка без
    единого пульса стоит в New и не тревожит — немым остался бы только лог."""
    pinger = HeartbeatPinger(url=HEARTBEAT_URL)
    with capture_logs() as logs:
        pinger.maybe_ping(_client(_respond(200, body)), now=0.0)

    failed = _events(logs, "alerter_heartbeat_failed")
    assert len(failed) == 1 and body in failed[0]["reason"]
    assert pinger.failing is True


def test_network_error_never_logs_the_check_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with capture_logs() as logs:
        HeartbeatPinger(url=HEARTBEAT_URL).maybe_ping(_client(handler), now=0.0)

    failed = _events(logs, "alerter_heartbeat_failed")
    assert len(failed) == 1 and failed[0]["reason"] == "ConnectError"
    assert CHECK_UUID not in repr(logs)


# ---------------------------------------------------------------------------
# Проводка в run_alerter
# ---------------------------------------------------------------------------


@pytest.fixture
def heartbeat_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", ALERT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ALERT_READY_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("ALERT_HEARTBEAT_URL", HEARTBEAT_URL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeStack:
    """/health/ready, /metrics, Telegram и проверка пульса в одном транспорте."""

    def __init__(self, ready_status: int = 200, telegram: httpx.Response | None = None) -> None:
        self.ready_status = ready_status
        self.telegram = telegram or httpx.Response(200, json={"ok": True})
        self.pings = 0
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "hc-ping.com":
            self.pings += 1
            return httpx.Response(200, text="OK")
        if request.url.path == "/health/ready":
            return httpx.Response(self.ready_status, json={"status": "x"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text="")
        if request.url.path.endswith("/sendMessage"):
            self.sent.append(json.loads(request.content))
            return self.telegram
        raise AssertionError(f"неожиданный запрос алертера: {request.url}")


def test_active_loop_sends_heartbeat(heartbeat_settings: None) -> None:
    stack = FakeStack()

    with capture_logs() as logs:
        run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    assert stack.pings == 1
    started = _events(logs, "alerter_started")
    assert started[0]["heartbeat"] == "включён"
    assert CHECK_UUID not in repr(logs)


def test_passive_loop_sends_no_heartbeat(
    heartbeat_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Алерты выключены — пульса нет: снаружи это и должно выглядеть как
    мёртвый алертер (issue #229)."""
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    get_settings.cache_clear()
    stack = FakeStack()

    run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    assert stack.pings == 0


def test_telegram_rejection_logs_reason_without_token(heartbeat_settings: None) -> None:
    """ERROR-лог алертера уходит в Sentry (#229): в нём диагноз от Telegram,
    но не адрес запроса — в адресе токен бота."""
    rejection = httpx.Response(
        403, json={"ok": False, "description": "Forbidden: bot was kicked from the channel chat"}
    )
    stack = FakeStack(ready_status=503, telegram=rejection)

    with capture_logs() as logs:
        run_alerter(iterations=1, transport=httpx.MockTransport(stack.handler))

    failed = _events(logs, "alert_send_failed")
    assert len(failed) == 1
    assert failed[0]["status_code"] == 403 and "bot was kicked" in failed[0]["detail"]
    assert "exc_info" not in failed[0]
    assert ALERT_TOKEN not in repr(logs)


def test_telegram_network_error_logs_class_without_token(heartbeat_settings: None) -> None:
    stack = FakeStack(ready_status=503)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return stack.handler(request)

    with capture_logs() as logs:
        run_alerter(iterations=1, transport=httpx.MockTransport(handler))

    failed = _events(logs, "alert_send_failed")
    assert len(failed) == 1 and failed[0]["error"] == "ConnectError"
    assert ALERT_TOKEN not in repr(logs)
