"""Сторож обещания ретеншна (issue #410, spec 0032 §5).

Маркер `guest_text_retention_completed` пишется и при сбое, поэтому проверяется
то, на что смотрят снаружи: счётчик упавших тенантов в нём, пульс
`guest-text-retention` только у прогона без сбоев и метрика возраста самого
старого сообщения — по ней алертер (ERR-OPS-017) видит и «удалено 0 строк».
Сценарии данных — помощники `test_retention.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from hospitality.channels.common import retention
from hospitality.channels.common.retention import (
    RETENTION_HEARTBEAT,
    enforce_guest_text_retention,
    refresh_guest_text_age_metrics,
)
from hospitality.channels.common.tests.test_retention import (
    FRESH,
    OLD,
    RETENTION_DAYS,
    _make_conversation,
)
from hospitality.shared.heartbeat import read_heartbeat_age_seconds
from hospitality.shared.metrics import set_guest_text_oldest_ages

_DAY = 86400.0
_METRIC = "guest_text_oldest_age_seconds"


def _age(tenant_id: uuid.UUID | str) -> float | None:
    return REGISTRY.get_sample_value(_METRIC, {"tenant_id": str(tenant_id)})


@pytest.fixture(autouse=True)
def _clean_metric() -> None:
    set_guest_text_oldest_ages({})


def _fail_on(monkeypatch: pytest.MonkeyPatch, broken: uuid.UUID) -> None:
    original = retention._enforce_tenant

    async def failing(tenant_id: uuid.UUID, cutoff: datetime) -> retention.RetentionRunStats:
        if tenant_id == broken:
            raise RuntimeError("connection reset")
        return await original(tenant_id, cutoff)

    monkeypatch.setattr(retention, "_enforce_tenant", failing)


async def test_failed_tenant_is_counted_and_retention_pulse_is_not_marked(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """«completed» пишется и при сбое — поэтому в нём счётчик упавших, а пульс
    `guest-text-retention` (прогон целиком) не ставится."""
    broken, _healthy = two_tenants
    _fail_on(monkeypatch, broken)

    with capture_logs() as logs:
        stats = await enforce_guest_text_retention(RETENTION_DAYS)

    completed = [log for log in logs if log["event"] == "guest_text_retention_completed"]
    assert stats.tenants_failed == 1
    assert len(completed) == 1 and completed[0]["tenants_failed"] == 1
    assert await read_heartbeat_age_seconds(RETENTION_HEARTBEAT) is None


async def test_clean_run_marks_retention_pulse(demo_tenant: uuid.UUID) -> None:
    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    age = await read_heartbeat_age_seconds(RETENTION_HEARTBEAT)
    assert stats.tenants_failed == 0
    assert age is not None and age < 60


async def test_age_metric_reports_oldest_message_per_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Возраст — самого старого сообщения своего тенанта (RLS, P-4); тенант без
    сообщений — нулём, а не пропуском: иначе алерт после починки не погас бы."""
    with_texts, without_texts = two_tenants
    await _make_conversation(
        with_texts, external_id="a", updated_days_ago=FRESH, message_ages_days=(OLD, FRESH)
    )

    await refresh_guest_text_age_metrics()

    age = _age(with_texts)
    assert age is not None and OLD * _DAY <= age < OLD * _DAY + 60
    assert _age(without_texts) == 0.0


async def test_age_metric_drops_below_the_term_after_retention(demo_tenant: uuid.UUID) -> None:
    """Метрика смотрит на результат: после прогона старейший текст — свежий."""
    await _make_conversation(
        demo_tenant, external_id="a", updated_days_ago=FRESH, message_ages_days=(OLD, FRESH)
    )
    await enforce_guest_text_retention(RETENTION_DAYS)

    await refresh_guest_text_age_metrics()

    age = _age(demo_tenant)
    assert age is not None and age < RETENTION_DAYS * _DAY


async def test_unreadable_database_clears_the_snapshot(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """БД не ответила — снимок стёрт целиком: для алертера это «не знаю», а не
    вчерашние числа и не нули."""
    set_guest_text_oldest_ages({str(demo_tenant): OLD * _DAY})

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[None]:
        raise OSError("connection refused")
        yield

    monkeypatch.setattr(retention, "platform_session_scope", broken_scope)

    await refresh_guest_text_age_metrics()  # не бросает

    assert _age(demo_tenant) is None
