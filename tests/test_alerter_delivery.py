"""Issue #288: состояние алерта переключается только после доставки в Telegram.

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``), и на
канале с заданными исходами; канал Telegram и цикл — на httpx.MockTransport.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, MutableMapping
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from hospitality.shared.config import Settings, get_settings
from hospitality.tools.alerter import (
    ERR_BACKUP_STALE,
    ERR_ERROR_SPIKE,
    ERR_LLM_BUDGET_NEAR_LIMIT,
    ERR_OUTBOX_BACKLOG,
    ERR_READY_UNAVAILABLE,
    ERR_WORKER_STALLED,
    ProbeResult,
    run_alerter,
)
from hospitality.tools.alerter_delivery import SendOutcome, TelegramAlertChannel
from hospitality.tools.alerter_retention import ERR_GUEST_TEXT_OVERDUE
from tests.test_alerter import (
    SAMPLE_METRICS,
    TENANT_A,
    backup_probe,
    budget_usage,
    make_monitor,
    ready_probe,
)

RETRY = SendOutcome.RETRY
REJECTED = SendOutcome.REJECTED


class ScriptedChannel:
    """Канал с заданными исходами по очереди; кончились — всё доставляется."""

    def __init__(self, *outcomes: SendOutcome) -> None:
        self.outcomes = list(outcomes)
        self.attempts: list[str] = []

    def send(self, text: str) -> SendOutcome:
        self.attempts.append(text)
        return self.outcomes.pop(0) if self.outcomes else SendOutcome.DELIVERED


def stale_backup() -> ProbeResult:
    return ready_probe(ok=True, backup=backup_probe(age_hours=41))


# ---------------------------------------------------------------------------
# Машина: AlertMonitor.deliver
# ---------------------------------------------------------------------------


def test_failed_send_postpones_state_alert_instead_of_dropping_it() -> None:
    """Сценарий issue: минута без Telegram не хоронит ERR-OPS-008 «свежего бэкапа нет»."""
    monitor = make_monitor()
    down = ScriptedChannel(RETRY)
    monitor.deliver(stale_backup(), now=0.0, send=down.send)
    assert len(down.attempts) == 1

    up = ScriptedChannel()
    monitor.deliver(stale_backup(), now=60.0, send=up.send)
    monitor.deliver(stale_backup(), now=120.0, send=up.send)

    assert len(up.attempts) == 1  # доставлен — и дальше один раз на вход в проблему
    assert ERR_BACKUP_STALE in up.attempts[0]


def test_problem_gone_before_channel_is_back_sends_neither_alert_nor_recovery() -> None:
    monitor = make_monitor()  # ERR-OPS-001 — со второго неуспешного опроса подряд
    channel = ScriptedChannel(RETRY)
    for probe in (ready_probe(ok=False), ready_probe(ok=False), ready_probe(ok=True)):
        monitor.deliver(probe, now=0.0, send=channel.send)
    monitor.deliver(ready_probe(ok=False), now=0.0, send=channel.send)

    assert len(channel.attempts) == 1  # только неудачная попытка; ✅ не о чем
    assert ERR_READY_UNAVAILABLE in channel.attempts[0]


def test_failed_recovery_message_is_sent_on_next_poll() -> None:
    monitor = make_monitor()
    channel = ScriptedChannel(SendOutcome.DELIVERED, RETRY)
    for probe in (ready_probe(ok=False), ready_probe(ok=False), ready_probe(ok=True)):
        monitor.deliver(probe, now=0.0, send=channel.send)
    monitor.deliver(ready_probe(ok=True), now=0.0, send=channel.send)
    monitor.deliver(ready_probe(ok=True), now=0.0, send=channel.send)

    alert, lost_recovery, recovery = channel.attempts
    assert "🔴" in alert and "✅" in lost_recovery and recovery == lost_recovery


def test_rejected_send_is_not_retried() -> None:
    """4xx Bot API повтором не лечится: состояние остаётся, ERROR — в Sentry."""
    monitor = make_monitor()
    channel = ScriptedChannel(REJECTED)
    monitor.deliver(stale_backup(), now=0.0, send=channel.send)
    monitor.deliver(stale_backup(), now=60.0, send=channel.send)

    assert len(channel.attempts) == 1


def test_channel_down_costs_one_attempt_per_poll_and_keeps_every_line() -> None:
    """Канал лежит: каждая попытка — до 5 с таймаута, поэтому одна за опрос, а все
    линии с сообщениями откатываются и уходят, когда канал вернулся."""
    probe = ready_probe(
        ok=True, heartbeat_age=900.0, outbox_depth=500.0, backup=backup_probe(age_hours=41)
    )
    monitor = make_monitor()
    down = ScriptedChannel(RETRY)
    monitor.deliver(probe, now=0.0, send=down.send)
    assert len(down.attempts) == 1

    up = ScriptedChannel()
    monitor.deliver(probe, now=60.0, send=up.send)

    assert len(up.attempts) == 3
    for code in (ERR_WORKER_STALLED, ERR_OUTBOX_BACKLOG, ERR_BACKUP_STALE):
        assert any(code in text for text in up.attempts), code


def test_rollback_undoes_only_the_line_that_failed() -> None:
    """Счётчик ERR-OPS-001 копится, пока соседняя линия ждёт канал: откат одной
    линии не отматывает остальные."""
    monitor = make_monitor()
    channel = ScriptedChannel(RETRY)
    monitor.deliver(
        ready_probe(ok=False, backup=backup_probe(age_hours=41)), now=0.0, send=channel.send
    )
    monitor.deliver(
        ready_probe(ok=False, backup=backup_probe(age_hours=41)), now=60.0, send=channel.send
    )

    delivered = channel.attempts[1:]
    assert len(delivered) == 2
    assert ERR_READY_UNAVAILABLE in delivered[0] and ERR_BACKUP_STALE in delivered[1]


@pytest.mark.parametrize(
    ("probe", "code"),
    [
        (budget_usage(9.0), ERR_LLM_BUDGET_NEAR_LIMIT),
        (ready_probe(ok=True, guest_text_ages={TENANT_A: 100 * 86400.0}), ERR_GUEST_TEXT_OVERDUE),
    ],
)
def test_rollback_restores_per_tenant_state(probe: ProbeResult, code: str) -> None:
    """Состояние по тенантам — множества, в том числе внутри вложенной линии:
    снимок обязан быть глубоким, иначе откат их не вернёт."""
    monitor = make_monitor()
    channel = ScriptedChannel(RETRY)
    monitor.deliver(probe, now=0.0, send=channel.send)
    monitor.deliver(probe, now=60.0, send=channel.send)

    assert len(channel.attempts) == 2 and code in channel.attempts[1]


def test_postponed_spike_alert_counts_from_the_old_baseline() -> None:
    monitor = make_monitor()  # всплеск 5xx — от +5 за опрос
    channel = ScriptedChannel(RETRY)
    for now, total in ((0.0, 100.0), (60.0, 110.0), (120.0, 112.0)):
        monitor.deliver(ready_probe(ok=True, errors_total=total), now=now, send=channel.send)

    assert len(channel.attempts) == 2
    assert ERR_ERROR_SPIKE in channel.attempts[1] and "+12 ответов" in channel.attempts[1]


# ---------------------------------------------------------------------------
# Канал: исход по ответу Bot API и логи серии сбоев
# ---------------------------------------------------------------------------


@pytest.fixture
def alert_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    monkeypatch.setenv("ALERT_READY_FAILURE_THRESHOLD", "2")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _channel(settings: Settings, *responses: httpx.Response | Exception) -> TelegramAlertChannel:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return TelegramAlertChannel(
        client=httpx.Client(transport=httpx.MockTransport(handler)), settings=settings
    )


def _events(logs: list[MutableMapping[str, Any]], name: str) -> list[MutableMapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (httpx.Response(200, json={"ok": True}), SendOutcome.DELIVERED),
        (httpx.Response(429, json={"ok": False, "description": "Too Many Requests"}), RETRY),
        (httpx.Response(500, json={"ok": False}), RETRY),
        (httpx.Response(502, text="<html>Bad Gateway</html>"), RETRY),
        (httpx.ConnectError("no route"), RETRY),
        (httpx.ReadTimeout("timed out"), RETRY),
        (httpx.Response(403, json={"ok": False, "description": "bot was kicked"}), REJECTED),
        (httpx.Response(400, json={"ok": False, "description": "chat not found"}), REJECTED),
    ],
)
def test_send_outcome_follows_bot_api_answer(
    alert_settings: Settings, answer: httpx.Response | Exception, outcome: SendOutcome
) -> None:
    assert _channel(alert_settings, answer).send("алерт") is outcome


def test_failure_streak_is_one_error_for_sentry(alert_settings: Settings) -> None:
    """Часовой сбой Telegram — одно событие Sentry, а не шестьдесят."""
    channel = _channel(
        alert_settings,
        httpx.Response(502, text="Bad Gateway"),
        httpx.ConnectError("no route"),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(500, json={"ok": False}),
    )
    with capture_logs() as logs:
        for _ in range(4):
            channel.send("алерт")

    failed = _events(logs, "alert_send_failed")
    assert len(failed) == 2  # вход в первую серию и во вторую
    assert all(entry["log_level"] == "error" and entry["retry"] is True for entry in failed)
    assert [entry["log_level"] for entry in _events(logs, "alert_send_retry")] == ["warning"]
    assert len(_events(logs, "alert_send_recovered")) == 1


def test_every_rejection_is_an_error(alert_settings: Settings) -> None:
    rejection = httpx.Response(403, json={"ok": False, "description": "bot was kicked"})
    channel = _channel(alert_settings, rejection, rejection)
    with capture_logs() as logs:
        channel.send("первый")
        channel.send("второй")

    failed = _events(logs, "alert_send_failed")
    assert [entry["retry"] for entry in failed] == [False, False]
    assert all(entry["log_level"] == "error" for entry in failed)


# ---------------------------------------------------------------------------
# Цикл run_alerter: тракт «опрос → машина → канал» с упавшим Telegram
# ---------------------------------------------------------------------------


def test_run_alerter_resends_alert_after_telegram_outage(alert_settings: Settings) -> None:
    telegram = [httpx.Response(500, json={"ok": False})]
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(503, json={"status": "unavailable"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=SAMPLE_METRICS)
        sent.append(json.loads(request.content)["text"])
        return telegram.pop(0) if telegram else httpx.Response(200, json={"ok": True})

    run_alerter(iterations=4, transport=httpx.MockTransport(handler))

    # Опрос 2 — алерт и 500 от Telegram, опрос 3 — тот же алерт доставлен, опрос 4 — тишина.
    assert len(sent) == 2
    assert all(ERR_READY_UNAVAILABLE in text for text in sent)
