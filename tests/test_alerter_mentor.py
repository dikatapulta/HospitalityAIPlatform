"""«Наставник» не отвечает сотрудникам — линии алертера (issue #911, ERR-OPS-018/019).

Машина — на снимках, собранных руками (помощники ``tests/test_alerter.py``); имена
метрик — по настоящей выдаче ``shared/metrics``; проводка ``/metrics`` →
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
from hospitality.shared.metrics import (
    LlmCallOutcomes,
    set_llm_call_outcomes,
    set_llm_mentor_outcomes,
)
from hospitality.tools.alerter import (
    ERR_LLM_PROVIDER_FAILING,
    AlertMonitor,
    read_llm_call_outcomes,
    run_alerter,
)
from hospitality.tools.alerter_mentor import (
    ERR_MENTOR_BUDGET_EXHAUSTED,
    ERR_MENTOR_PROVIDER_FAILING,
    MentorOutcomes,
    read_mentor_outcomes,
)
from tests.test_alerter import TENANT_A, TENANT_B, make_monitor, ready_probe

QUIET = MentorOutcomes(provider_errors=0, provider_timeouts=0, budget_rejections=0)


def failing(errors: int = 0, timeouts: int = 0) -> MentorOutcomes:
    return MentorOutcomes(provider_errors=errors, provider_timeouts=timeouts, budget_rejections=0)


def rejecting(rejections: int) -> MentorOutcomes:
    return MentorOutcomes(provider_errors=0, provider_timeouts=0, budget_rejections=rejections)


def step(monitor: AlertMonitor, outcomes: dict[str, MentorOutcomes] | None) -> list[str]:
    return monitor.evaluate(ready_probe(ok=True, mentor_outcomes=outcomes), now=0.0)


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------


def test_parser_reads_the_metrics_the_application_writes() -> None:
    """Имена метрик и метки — по настоящей выдаче: переименованная метрика выключила
    бы линию молча. Гостевой парсер серии наставника не видит, и наоборот."""
    try:
        set_llm_mentor_outcomes(
            {
                TENANT_A: LlmCallOutcomes(
                    provider_errors=3, provider_timeouts=1, budget_rejections=0
                ),
                TENANT_B: LlmCallOutcomes(
                    provider_errors=0, provider_timeouts=0, budget_rejections=2
                ),
            }
        )
        set_llm_call_outcomes({})
        text = generate_latest(REGISTRY).decode()

        assert read_mentor_outcomes(text) == {
            TENANT_A: MentorOutcomes(provider_errors=3, provider_timeouts=1, budget_rejections=0),
            TENANT_B: rejecting(2),
        }
        assert read_llm_call_outcomes(text) == []
    finally:
        set_llm_mentor_outcomes({})


def test_parser_skips_half_snapshot_and_nan() -> None:
    """Обе метрики публикуются вместе — тенант с одной из них это «не знаю»."""
    text = (
        f'llm_mentor_failure_streak{{status="error",tenant_id="{TENANT_A}"}} 3.0\n'
        f'llm_mentor_failure_streak{{status="timeout",tenant_id="{TENANT_A}"}} 0.0\n'
        f'llm_mentor_budget_rejection_streak{{tenant_id="{TENANT_B}"}} NaN\n'
    )

    assert read_mentor_outcomes(text) == {}


# ---------------------------------------------------------------------------
# ERR-OPS-018 — провайдер наставника отказывает серией
# ---------------------------------------------------------------------------


def test_provider_streak_alerts_once_and_recovers() -> None:
    monitor = make_monitor(llm_failure_streak_threshold=3)

    below = step(monitor, {TENANT_A: failing(errors=2)})
    entered = step(monitor, {TENANT_A: failing(errors=3)})
    held = step(monitor, {TENANT_A: failing(errors=5)})
    recovered = step(monitor, {TENANT_A: QUIET})

    assert below == []
    assert len(entered) == 1 and entered[0].startswith("🔴")
    assert ERR_MENTOR_PROVIDER_FAILING in entered[0] and f"tenant: {TENANT_A}" in entered[0]
    assert "«Наставник» не отвечает сотрудникам" in entered[0]
    assert "OpenAI Platform" in entered[0]
    assert ERR_LLM_PROVIDER_FAILING not in entered[0]
    assert held == []
    assert len(recovered) == 1 and recovered[0].startswith("✅")
    assert "«Наставник» снова отвечает" in recovered[0]


def test_timeouts_only_point_to_provider_status() -> None:
    monitor = make_monitor(llm_failure_streak_threshold=3)

    [message] = step(monitor, {TENANT_A: failing(timeouts=3)})

    assert "status.openai.com" in message and "Billing" not in message


def test_zero_threshold_switches_provider_line_off() -> None:
    monitor = make_monitor(llm_failure_streak_threshold=0)

    assert step(monitor, {TENANT_A: failing(errors=10)}) == []


def test_mentor_failures_do_not_raise_guest_alert() -> None:
    """Серия наставника будит только ERR-OPS-018 — гостевая линия о ней не знает."""
    monitor = make_monitor()

    messages = step(monitor, {TENANT_A: failing(errors=3)})

    assert all(ERR_LLM_PROVIDER_FAILING not in message for message in messages)


# ---------------------------------------------------------------------------
# ERR-OPS-019 — дневной потолок наставника выбран
# ---------------------------------------------------------------------------


def test_budget_exhausted_alerts_on_first_rejection_and_recovers_with_count() -> None:
    monitor = make_monitor()

    entered = step(monitor, {TENANT_A: rejecting(1)})
    held = step(monitor, {TENANT_A: rejecting(4)})
    recovered = step(monitor, {TENANT_A: QUIET})

    assert len(entered) == 1 and ERR_MENTOR_BUDGET_EXHAUSTED in entered[0]
    assert "LLM_MENTOR_DAILY_BUDGET_USD" in entered[0]
    assert held == []
    assert len(recovered) == 1 and recovered[0].startswith("✅")
    assert "не меньше 4" in recovered[0]


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------


def test_unknown_snapshot_keeps_state() -> None:
    """/metrics не ответил (None) или снимок стёрт (БД) — ни алерта, ни ✅."""
    monitor = make_monitor()
    step(monitor, {TENANT_A: failing(errors=3)})

    assert step(monitor, None) == []
    assert step(monitor, {}) == []
    assert step(monitor, {TENANT_A: failing(errors=4)}) == []


def test_state_is_per_tenant() -> None:
    monitor = make_monitor()

    first = step(monitor, {TENANT_A: rejecting(1), TENANT_B: QUIET})
    second = step(monitor, {TENANT_A: rejecting(2), TENANT_B: rejecting(1)})

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
    monkeypatch.setenv("ALERT_LLM_FAILURE_STREAK_THRESHOLD", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_run_alerter_reports_mentor_failures_from_metrics(alerter_env: None) -> None:
    """Тракт «HTTP-ответ → парсер → машина → отправка» и порог из Settings: тесты
    машины собирают снимок руками, и снятая строка проводки краснеет только здесь."""
    metrics = (
        f'llm_mentor_failure_streak{{status="error",tenant_id="{TENANT_A}"}} 2.0\n'
        f'llm_mentor_failure_streak{{status="timeout",tenant_id="{TENANT_A}"}} 0.0\n'
        f'llm_mentor_budget_rejection_streak{{tenant_id="{TENANT_A}"}} 0.0\n'
    )
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=metrics)
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"неожиданный запрос алертера: {request.url}")

    run_alerter(iterations=1, transport=httpx.MockTransport(handler))

    assert len(sent) == 1, sent
    assert ERR_MENTOR_PROVIDER_FAILING in sent[0]["text"]
    assert f"tenant: {TENANT_A}" in sent[0]["text"]
