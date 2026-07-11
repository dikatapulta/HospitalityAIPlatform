"""Тесты воркера (Task 0010, ADR-005): сквозная доставка и живучесть цикла.

Механика доставки (атомарность, ретраи, идемпотентность) — в test_events.py;
здесь — composition root воркера: регистрация подписчиков, полный путь
«публикация → outbox → цикл воркера → эффект подписчика» и устойчивость
цикла к инфраструктурным сбоям.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from hospitality.platform.models import Tenant, TenantIsolationCanary
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.publish_demo_event import DEMO_TENANT_SLUG, publish_demo_event
from hospitality.worker import run_worker


async def test_worker_delivers_demo_event_end_to_end(canonical_database: None) -> None:
    """Тот же сценарий, что smoke на staging (runbook deploy): демо-публикация
    создаёт тенанта и канарейку, цикл воркера доставляет событие каноническому
    подписчику — появляется echo-строка того же тенанта."""
    correlation_id = await publish_demo_event()
    assert correlation_id

    await run_worker(iterations=1)

    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == DEMO_TENANT_SLUG))
    assert tenant_id is not None
    with tenant_context(tenant_id):
        async with session_scope() as session:
            echo_count = await session.scalar(
                select(func.count())
                .select_from(TenantIsolationCanary)
                .where(TenantIsolationCanary.note.like("echo:%"))
            )
    assert echo_count == 1


async def test_worker_iteration_survives_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступная БД / неприменённые миграции не роняют процесс: итерация
    логирует ERR-EVENTS-003 и повторяется после паузы (см. worker.run_worker)."""

    async def broken_delivery(
        batch_size: int | None = None, max_attempts: int | None = None
    ) -> int:
        raise RuntimeError("db is down")

    monkeypatch.setattr("hospitality.worker.deliver_pending_events", broken_delivery)
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_runs_cleanup_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention-очистка outbox (issue #18, ADR-009) вызывается из цикла воркера,
    когда с прошлой попытки прошло не меньше worker_cleanup_interval_seconds."""
    calls = 0

    async def fake_cleanup(retention_days: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.cleanup_processed_events", fake_cleanup)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=1)
    finally:
        get_settings.cache_clear()
    assert calls == 1


async def test_worker_survives_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-EVENTS-004 (docs/runbooks/errors.md): сбой retention-очистки логируется
    и не роняет цикл воркера — доставка продолжается на следующей итерации."""

    async def broken_cleanup(retention_days: int | None = None) -> int:
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.cleanup_processed_events", broken_cleanup)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_skips_cleanup_before_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обратная сторона предыдущего теста: пока интервал не истёк, очистка не
    вызывается на каждой итерации — иначе холостой DELETE бил бы БД ежесекундно."""
    calls = 0

    async def fake_cleanup(retention_days: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.cleanup_processed_events", fake_cleanup)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()
    assert calls == 0
