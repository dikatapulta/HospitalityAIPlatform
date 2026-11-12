"""Напоминание о незакрытом алерте состояния на копии отеля (issue #1050, часть 3).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``); каждая
линия состояния проверяется сквозь ``AlertMonitor``: открылась → через час 🔁 с её
кодом. Проводка ``ALERT_STATE_REPEAT_SECONDS`` и метки контура → ``run_alerter`` —
на ``httpx.MockTransport``.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from hospitality.shared.config import get_settings
from hospitality.tools.alerter import (
    ERR_BACKUP_STALE,
    ERR_LLM_BUDGET_EXHAUSTED,
    ERR_LLM_BUDGET_NEAR_LIMIT,
    ERR_LLM_PROVIDER_FAILING,
    ERR_OUTBOX_BACKLOG,
    ERR_READY_UNAVAILABLE,
    ERR_WORKER_STALLED,
    ProbeResult,
    run_alerter,
)
from hospitality.tools.alerter_delivery import SendOutcome
from hospitality.tools.alerter_disk import ERR_DISK_LOW, DiskProbe
from hospitality.tools.alerter_mentor import (
    ERR_MENTOR_BUDGET_EXHAUSTED,
    ERR_MENTOR_PROVIDER_FAILING,
    MentorOutcomes,
)
from hospitality.tools.alerter_offsite import ERR_OFFSITE_STALE, OffsiteProbe
from hospitality.tools.alerter_repeat import (
    ActiveStateAlert,
    StateAlertRepeater,
    state_repeat_seconds,
)
from hospitality.tools.alerter_retention import ERR_GUEST_TEXT_OVERDUE
from hospitality.tools.alerter_scrape import ERR_METRICS_UNAVAILABLE
from hospitality.tools.alerter_unclaimed import ERR_REQUESTS_UNCLAIMED, ForgottenRequests
from tests.test_alerter import (
    TENANT_A,
    TENANT_B,
    FakeStagingStack,
    backup_probe,
    budget_usage,
    call_outcomes,
    make_monitor,
    ready_probe,
)

_HOUR = 3600.0
_RUNBOOK = "https://example.invalid/alerts.md"


def reminders(messages: list[str]) -> list[str]:
    return [message for message in messages if message.startswith("🔁")]


# ---------------------------------------------------------------------------
# Машина напоминаний
# ---------------------------------------------------------------------------


def worker_alert(tenant: str = "platform") -> ActiveStateAlert:
    return ActiveStateAlert(
        error_code=ERR_WORKER_STALLED,
        tenant=tenant,
        title="воркер событий не подаёт признаков жизни",
    )


def remind(repeater: StateAlertRepeater, active: list[ActiveStateAlert], now: float) -> list[str]:
    return repeater.evaluate(active, now=now, environment="pilot", runbook_url=_RUNBOOK)


def test_reminder_comes_once_per_interval_while_alert_stays_open() -> None:
    repeater = StateAlertRepeater(interval_seconds=_HOUR)

    opened = remind(repeater, [worker_alert()], now=0.0)  # о нём только что сказала линия
    early = remind(repeater, [worker_alert()], now=_HOUR - 60)
    first = remind(repeater, [worker_alert()], now=_HOUR)
    between = remind(repeater, [worker_alert()], now=_HOUR + 60)
    second = remind(repeater, [worker_alert()], now=2 * _HOUR)

    assert opened == [] and early == [] and between == []
    assert first == [
        "🔁 [pilot] ERR-OPS-003: без ✅ уже 1 ч — воркер событий не подаёт признаков жизни\n"
        "детали: напоминание о незакрытом алерте — сообщения ✅ о восстановлении не было. "
        "Следующее — через 1 ч, если ✅ не придёт\n"
        "tenant: platform · correlation_id: —\n"
        f"runbook: {_RUNBOOK}#err-ops-003"
    ]
    assert len(second) == 1 and "без ✅ уже 2 ч" in second[0]


def test_closed_alert_is_forgotten_and_reopened_one_counts_anew() -> None:
    repeater = StateAlertRepeater(interval_seconds=_HOUR)
    remind(repeater, [worker_alert()], now=0.0)

    closed = remind(repeater, [], now=30 * 60.0)  # ✅ ушло
    remind(repeater, [worker_alert()], now=40 * 60.0)  # открылся снова
    too_early = remind(repeater, [worker_alert()], now=_HOUR + 60)
    again = remind(repeater, [worker_alert()], now=40 * 60.0 + _HOUR)

    assert closed == [] and too_early == []
    assert len(again) == 1 and "без ✅ уже 1 ч" in again[0]


def test_each_tenant_has_its_own_reminder() -> None:
    repeater = StateAlertRepeater(interval_seconds=_HOUR)
    remind(repeater, [worker_alert(TENANT_A)], now=0.0)
    remind(repeater, [worker_alert(TENANT_A), worker_alert(TENANT_B)], now=30 * 60.0)

    only_a = remind(repeater, [worker_alert(TENANT_A), worker_alert(TENANT_B)], now=_HOUR)

    assert len(only_a) == 1 and f"tenant: {TENANT_A}" in only_a[0]


def test_zero_interval_turns_the_line_off() -> None:
    repeater = StateAlertRepeater(interval_seconds=0.0)

    assert remind(repeater, [worker_alert()], now=0.0) == []
    assert remind(repeater, [worker_alert()], now=100 * _HOUR) == []


def test_only_the_pilot_contour_repeats() -> None:
    assert state_repeat_seconds("pilot", _HOUR) == _HOUR
    assert state_repeat_seconds("staging", _HOUR) == 0.0
    assert state_repeat_seconds("dev", _HOUR) == 0.0


# ---------------------------------------------------------------------------
# Каждая линия состояния — сквозь AlertMonitor
# ---------------------------------------------------------------------------

_BROKEN: dict[str, tuple[ProbeResult, str]] = {
    ERR_READY_UNAVAILABLE: (ready_probe(ok=False), "platform"),
    ERR_METRICS_UNAVAILABLE: (
        ready_probe(ok=True, metrics_ok=False, metrics_detail="HTTP 502", errors_total=None),
        "platform",
    ),
    ERR_WORKER_STALLED: (ready_probe(ok=True, heartbeat_age=3 * _HOUR), "platform"),
    ERR_OUTBOX_BACKLOG: (ready_probe(ok=True, outbox_depth=500.0), "platform"),
    ERR_BACKUP_STALE: (ready_probe(ok=True, backup=backup_probe(age_hours=40)), "platform"),
    ERR_OFFSITE_STALE: (ready_probe(ok=True, offsite=OffsiteProbe(age_seconds=None)), "platform"),
    ERR_DISK_LOW: (
        ready_probe(ok=True, disk=DiskProbe(path="/backups", free_bytes=1, total_bytes=100)),
        "platform",
    ),
    ERR_LLM_PROVIDER_FAILING: (call_outcomes(errors=3), TENANT_A),
    ERR_LLM_BUDGET_EXHAUSTED: (call_outcomes(rejections=1), TENANT_A),
    ERR_MENTOR_PROVIDER_FAILING: (
        ready_probe(ok=True, mentor_outcomes={TENANT_A: MentorOutcomes(3, 0, 0)}),
        TENANT_A,
    ),
    ERR_MENTOR_BUDGET_EXHAUSTED: (
        ready_probe(ok=True, mentor_outcomes={TENANT_A: MentorOutcomes(0, 0, 1)}),
        TENANT_A,
    ),
    ERR_GUEST_TEXT_OVERDUE: (
        ready_probe(ok=True, guest_text_ages={TENANT_A: 200 * 24 * _HOUR}),
        TENANT_A,
    ),
    ERR_REQUESTS_UNCLAIMED: (
        ready_probe(ok=True, forgotten_requests={TENANT_A: ForgottenRequests(1, 0, 900.0)}),
        TENANT_A,
    ),
}


@pytest.mark.parametrize("code", sorted(_BROKEN))
def test_every_state_line_is_reminded_with_its_code(code: str) -> None:
    """Линия открыла алерт — через час 🔁 с её кодом и отелем. Новая линия состояния,
    забытая в ``active_state_alerts``, напоминания не получит — строка сюда."""
    probe, tenant = _BROKEN[code]
    monitor = make_monitor(
        environment="pilot", state_repeat_seconds=_HOUR, offsite_max_age_hours=48.0
    )

    opened = monitor.evaluate(probe, now=0.0) + monitor.evaluate(probe, now=60.0)
    later = monitor.evaluate(probe, now=60.0 + _HOUR)

    assert any(code in message and "🔴" in message for message in opened)
    assert reminders(opened) == []
    assert len(reminders(later)) == 1
    assert f"[pilot] {code}: без ✅ уже 1 ч" in later[0]
    assert f"tenant: {tenant}" in later[0]


def test_recovered_alert_is_not_reminded() -> None:
    monitor = make_monitor(environment="pilot", state_repeat_seconds=_HOUR)
    stalled = ready_probe(ok=True, heartbeat_age=3 * _HOUR)
    monitor.evaluate(stalled, now=0.0)

    recovered = monitor.evaluate(ready_probe(ok=True, heartbeat_age=5.0), now=600.0)
    later = monitor.evaluate(ready_probe(ok=True, heartbeat_age=5.0), now=2 * _HOUR)

    assert any("✅" in message for message in recovered)
    assert later == [] and monitor.active_state_alerts() == []


def test_budget_forecast_is_not_reminded() -> None:
    """ERR-OPS-006 — прогноз, висит до конца UTC-суток: напоминаний о нём нет."""
    monitor = make_monitor(environment="pilot", state_repeat_seconds=_HOUR)

    opened = monitor.evaluate(budget_usage(9.0), now=0.0)
    later = monitor.evaluate(budget_usage(9.0), now=5 * _HOUR)

    assert any(ERR_LLM_BUDGET_NEAR_LIMIT in message for message in opened)
    assert later == []


def test_unknown_metric_keeps_the_alert_open() -> None:
    """Пропала метрика — «не знаю», а не ✅: алерт открыт, напоминание идёт."""
    monitor = make_monitor(environment="pilot", state_repeat_seconds=_HOUR)
    monitor.evaluate(ready_probe(ok=True, heartbeat_age=3 * _HOUR), now=0.0)

    later = monitor.evaluate(ready_probe(ok=True, heartbeat_age=None), now=_HOUR)

    assert len(reminders(later)) == 1 and ERR_WORKER_STALLED in later[0]


def test_staging_monitor_never_reminds() -> None:
    monitor = make_monitor(environment="staging")
    stalled = ready_probe(ok=True, heartbeat_age=3 * _HOUR)
    monitor.evaluate(stalled, now=0.0)

    assert monitor.evaluate(stalled, now=10 * _HOUR) == []


def test_failed_delivery_retries_the_reminder_on_next_poll() -> None:
    """Telegram лёг на напоминании — оно не теряется, а уходит следующим опросом."""
    monitor = make_monitor(environment="pilot", state_repeat_seconds=_HOUR)
    stalled = ready_probe(ok=True, heartbeat_age=3 * _HOUR)
    sent: list[str] = []

    def up(text: str) -> SendOutcome:
        sent.append(text)
        return SendOutcome.DELIVERED

    monitor.deliver(stalled, now=0.0, send=up)
    monitor.deliver(stalled, now=_HOUR, send=lambda _text: SendOutcome.RETRY)
    monitor.deliver(stalled, now=_HOUR + 60, send=up)
    monitor.deliver(stalled, now=_HOUR + 120, send=up)

    assert len(sent) == 2
    assert "🔴" in sent[0] and reminders(sent[1:]) == sent[1:]


# ---------------------------------------------------------------------------
# Проводка run_alerter: метка контура и ALERT_STATE_REPEAT_SECONDS
# ---------------------------------------------------------------------------


@pytest.fixture
def pilot_alerter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "1e-6")
    monkeypatch.setenv("ALERT_READY_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("ALERT_STATE_REPEAT_SECONDS", "1e-6")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(("environment", "expected"), [("pilot", 1), ("staging", 0)])
def test_run_alerter_reminds_only_on_pilot(
    pilot_alerter: None, monkeypatch: pytest.MonkeyPatch, environment: str, expected: int
) -> None:
    monkeypatch.setenv("SENTRY_ENVIRONMENT", environment)
    get_settings.cache_clear()
    stack = FakeStagingStack(ready_statuses=[503, 503, 503])

    run_alerter(iterations=3, transport=httpx.MockTransport(stack.handler))

    texts = [message["text"] for message in stack.sent_messages]
    assert texts[0].startswith(f"🔴 [{environment}] {ERR_READY_UNAVAILABLE}")
    assert len(reminders(texts)) == expected
