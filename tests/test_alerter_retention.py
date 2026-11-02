"""Тексты гостей старше срока — линия алертера (issue #410, ERR-OPS-017).

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
from hospitality.shared.metrics import set_guest_text_oldest_ages
from hospitality.tools.alerter import AlertMonitor, run_alerter
from hospitality.tools.alerter_retention import ERR_GUEST_TEXT_OVERDUE, read_guest_text_ages
from tests.test_alerter import TENANT_A, TENANT_B, make_monitor, ready_probe

_DAY = 86400.0
# make_monitor: срок 90 дней и запас 48 часов — порог 92 дня.
_OVERDUE = 93 * _DAY
_YOUNG = 30 * _DAY


def step(monitor: AlertMonitor, ages: dict[str, float] | None) -> list[str]:
    return monitor.evaluate(ready_probe(ok=True, guest_text_ages=ages), now=0.0)


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------


def test_parser_reads_the_metric_the_application_writes() -> None:
    """Имя метрики и метка тенанта — по настоящей выдаче, а не по строке из головы:
    переименованная метрика выключила бы линию молча."""
    try:
        set_guest_text_oldest_ages({TENANT_A: _OVERDUE, TENANT_B: 0.0})

        ages = read_guest_text_ages(generate_latest(REGISTRY).decode())
    finally:
        set_guest_text_oldest_ages({})

    assert ages == {TENANT_A: _OVERDUE, TENANT_B: 0.0}


def test_parser_drops_nan() -> None:
    text = f'guest_text_oldest_age_seconds{{tenant_id="{TENANT_A}"}} NaN\n'

    assert read_guest_text_ages(text) == {}


# ---------------------------------------------------------------------------
# Машина состояний
# ---------------------------------------------------------------------------


def test_overdue_text_alerts_once_and_recovers_with_check_mark() -> None:
    """Алерт состояния: одно сообщение на вход, тишина, пока проблема держится,
    ✅ на выход — как ERR-OPS-003."""
    monitor = make_monitor()

    entered = step(monitor, {TENANT_A: _OVERDUE})
    held = step(monitor, {TENANT_A: _OVERDUE + _DAY})
    recovered = step(monitor, {TENANT_A: _YOUNG})

    assert len(entered) == 1 and ERR_GUEST_TEXT_OVERDUE in entered[0]
    assert entered[0].startswith("🔴") and f"tenant: {TENANT_A}" in entered[0]
    assert "93 дн" in entered[0]
    assert held == []
    assert len(recovered) == 1 and recovered[0].startswith("✅")
    assert "30 дн" in recovered[0]


def test_threshold_is_retention_plus_grace() -> None:
    """Старше 90 дней, но в пределах запаса — ещё не алерт: ретеншн идёт раз в час."""
    monitor = make_monitor()

    assert step(monitor, {TENANT_A: 91 * _DAY}) == []
    assert len(step(monitor, {TENANT_A: 92 * _DAY + 60})) == 1


def test_tenant_without_messages_clears_the_alert() -> None:
    """Ретеншн удалил всё — тенант публикуется нулём, и алерт гаснет."""
    monitor = make_monitor()
    step(monitor, {TENANT_A: _OVERDUE})

    recovered = step(monitor, {TENANT_A: 0.0})

    assert len(recovered) == 1 and "сообщений нет" in recovered[0]


def test_unknown_snapshot_keeps_state() -> None:
    """/metrics не ответил (None) или снимок стёрт (БД) — «не знаю»: ни алерта, ни ✅."""
    monitor = make_monitor()
    step(monitor, {TENANT_A: _OVERDUE})

    assert step(monitor, None) == []
    assert step(monitor, {}) == []
    assert step(monitor, {TENANT_A: _OVERDUE}) == []  # состояние не сброшено


def test_state_is_per_tenant() -> None:
    monitor = make_monitor()

    first = step(monitor, {TENANT_A: _OVERDUE, TENANT_B: _YOUNG})
    second = step(monitor, {TENANT_A: _OVERDUE, TENANT_B: _OVERDUE})

    assert len(first) == 1 and f"tenant: {TENANT_A}" in first[0]
    assert len(second) == 1 and f"tenant: {TENANT_B}" in second[0]


# ---------------------------------------------------------------------------
# Проводка /metrics → run_alerter
# ---------------------------------------------------------------------------


@pytest.fixture
def alerter_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_run_alerter_reports_overdue_guest_text_from_metrics(
    alerter_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тракт «HTTP-ответ → парсер → машина → отправка» и порог из Settings: тесты
    машины собирают снимок руками, и снятая строка проводки краснеет только здесь.
    Срок 10 дней и запас 1 час — тексту в 10 дней и 2 часа уже пора."""
    monkeypatch.setenv("MESSAGES_RETENTION_DAYS", "10")
    monkeypatch.setenv("ALERT_GUEST_TEXT_RETENTION_GRACE_HOURS", "1")
    get_settings.cache_clear()
    age = 10 * _DAY + 2 * 3600
    metrics = f'guest_text_oldest_age_seconds{{tenant_id="{TENANT_A}"}} {age}\n'
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
    assert ERR_GUEST_TEXT_OVERDUE in sent[0]["text"]
    assert "дольше обещанных 10 дней" in sent[0]["text"]
    assert f"tenant: {TENANT_A}" in sent[0]["text"]
