"""Тесты канона доменных событий (Task 0010, P-6, P-8, ADR-005).

Атомарность публикации с бизнес-записью, доставка подписчикам в контексте
тенанта, идемпотентность повторной доставки, ретраи/исчерпание попыток и RLS
на outbox. Всё — на живом Postgres через канонические scope'ы, тем же путём,
которым ходит весь код платформы.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest
import structlog
from sqlalchemy import func, select

from hospitality.platform.events import CanaryCreated, echo_canary_created
from hospitality.platform.models import Tenant, TenantIsolationCanary
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.events import (
    DomainEvent,
    OutboxEvent,
    deliver_pending_events,
    publish,
    subscribe,
)
from hospitality.shared.tenancy import (
    TenantContextRequiredError,
    current_tenant_id,
    tenant_context,
)


class NoteAdded(DomainEvent):
    """Тестовое событие с минимальной нагрузкой."""

    event_name: ClassVar[str] = "test.note_added"

    note: str


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B»."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def _publish_note(tenant_id: uuid.UUID, note: str) -> None:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await publish(session, NoteAdded(note=note))


async def _single_outbox_row() -> OutboxEvent:
    async with platform_session_scope() as session:
        return (await session.execute(select(OutboxEvent))).scalar_one()


# ---------------------------------------------------------------------------
# Контракт DomainEvent и реестр подписчиков
# ---------------------------------------------------------------------------


def test_domain_event_requires_event_name() -> None:
    with pytest.raises(TypeError, match="event_name"):

        class Nameless(DomainEvent):
            note: str


def test_event_name_cannot_be_reused_by_another_class() -> None:
    class SameName(DomainEvent):
        event_name: ClassVar[str] = "test.note_added"

        other: int

    async def handle_note(event: NoteAdded) -> None:
        raise AssertionError("не должен вызываться")

    async def handle_same_name(event: SameName) -> None:
        raise AssertionError("не должен вызываться")

    subscribe(NoteAdded, handle_note)
    with pytest.raises(ValueError, match="уже занят"):
        subscribe(SameName, handle_same_name)


# ---------------------------------------------------------------------------
# Публикация: атомарно с бизнес-записью, только в контексте тенанта
# ---------------------------------------------------------------------------


async def test_publish_requires_tenant_context(canonical_database: None) -> None:
    with pytest.raises(TenantContextRequiredError):
        async with platform_session_scope() as session:
            await publish(session, NoteAdded(note="no-context"))


async def test_publish_commits_atomically_with_business_write(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with (
        structlog.contextvars.bound_contextvars(correlation_id="corr-published"),
        tenant_context(tenant_a),
    ):
        async with session_scope() as session:
            session.add(TenantIsolationCanary(note="with-event"))
            await publish(session, NoteAdded(note="with-event"))

    row = await _single_outbox_row()
    assert row.event_name == "test.note_added"
    assert row.tenant_id == tenant_a
    assert row.payload == {"note": "with-event"}
    assert row.correlation_id == "corr-published"
    assert row.processed_at is None
    assert row.attempts == 0


async def test_publish_rolls_back_with_business_write(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Ядро карточки задачи: событие не может пережить откат бизнес-записи."""
    tenant_a, _ = two_tenants
    with pytest.raises(RuntimeError, match="expected"), tenant_context(tenant_a):
        async with session_scope() as session:
            session.add(TenantIsolationCanary(note="ghost"))
            await publish(session, NoteAdded(note="ghost"))
            await session.flush()
            raise RuntimeError("expected")

    async with platform_session_scope() as session:
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    with tenant_context(tenant_a):
        async with session_scope() as session:
            canary_count = await session.scalar(
                select(func.count()).select_from(TenantIsolationCanary)
            )
    assert outbox_count == 0
    assert canary_count == 0


# ---------------------------------------------------------------------------
# Доставка: контекст тенанта, correlation id, отметка processed_at
# ---------------------------------------------------------------------------


async def test_delivery_runs_handler_in_tenant_and_correlation_context(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    seen: list[tuple[str, uuid.UUID, object]] = []

    async def record_note(event: NoteAdded) -> None:
        seen.append(
            (
                event.note,
                current_tenant_id(),
                structlog.contextvars.get_contextvars().get("correlation_id"),
            )
        )

    subscribe(NoteAdded, record_note)
    with structlog.contextvars.bound_contextvars(correlation_id="corr-delivery"):
        await _publish_note(tenant_a, "hello")
    structlog.contextvars.clear_contextvars()  # доставка восстанавливает id сама

    assert await deliver_pending_events() == 1
    assert seen == [("hello", tenant_a, "corr-delivery")]

    row = await _single_outbox_row()
    assert row.processed_at is not None
    assert row.attempts == 1
    assert row.last_error is None


async def test_all_subscribers_receive_event(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    received: list[str] = []

    async def first_handler(event: NoteAdded) -> None:
        received.append("first")

    async def second_handler(event: NoteAdded) -> None:
        received.append("second")

    subscribe(NoteAdded, first_handler)
    subscribe(NoteAdded, second_handler)
    await _publish_note(tenant_a, "fan-out")

    assert await deliver_pending_events() == 1
    assert received == ["first", "second"]


async def test_event_without_subscribers_is_marked_processed(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    await _publish_note(tenant_a, "nobody-listens")

    assert await deliver_pending_events() == 1
    row = await _single_outbox_row()
    assert row.processed_at is not None


# ---------------------------------------------------------------------------
# Надёжность: событие переживает падение, повторы не дублируют эффект (P-8)
# ---------------------------------------------------------------------------


async def test_event_survives_handler_crash_and_is_retried(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Карточка задачи: «событие переживает падение воркера» — упавшая доставка
    оставляет событие в outbox с диагнозом, следующий цикл доставляет его."""
    tenant_a, _ = two_tenants
    calls: list[str] = []

    async def flaky_handler(event: NoteAdded) -> None:
        calls.append(event.note)
        if len(calls) == 1:
            raise RuntimeError("first delivery crashes")

    subscribe(NoteAdded, flaky_handler)
    await _publish_note(tenant_a, "retry-me")

    assert await deliver_pending_events() == 1  # попытка была и упала
    row = await _single_outbox_row()
    assert row.processed_at is None
    assert row.attempts == 1
    assert row.last_error is not None
    assert "first delivery crashes" in row.last_error

    assert await deliver_pending_events() == 1  # событие пережило падение
    row = await _single_outbox_row()
    assert row.processed_at is not None
    assert calls == ["retry-me", "retry-me"]


async def test_delivery_attempts_are_capped(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants

    async def poison_handler(event: NoteAdded) -> None:
        raise RuntimeError("poison")

    subscribe(NoteAdded, poison_handler)
    await _publish_note(tenant_a, "poison")

    assert await deliver_pending_events(max_attempts=2) == 1
    assert await deliver_pending_events(max_attempts=2) == 1
    # Попытки исчерпаны: событие больше не берётся в работу, но остаётся
    # в outbox с диагнозом (разбор — ERR-EVENTS-002 в каталоге ошибок).
    assert await deliver_pending_events(max_attempts=2) == 0
    row = await _single_outbox_row()
    assert row.processed_at is None
    assert row.attempts == 2


async def test_redelivery_does_not_duplicate_effect(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Карточка задачи: «повторная доставка не дублирует эффект» — проверяется
    на каноническом подписчике `echo_canary_created` (образец P-8)."""
    tenant_a, _ = two_tenants
    subscribe(CanaryCreated, echo_canary_created)

    with tenant_context(tenant_a):
        async with session_scope() as session:
            canary = TenantIsolationCanary(note="original")
            session.add(canary)
            await session.flush()
            await publish(session, CanaryCreated(canary_id=canary.id, note=canary.note))
            canary_id = canary.id

    assert await deliver_pending_events() == 1
    # Худший сценарий at-least-once: эффект записан, отметка о доставке — нет
    # (процесс «упал» между ними). Возвращаем событие в очередь и доставляем снова.
    async with platform_session_scope() as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
        row.processed_at = None
    assert await deliver_pending_events() == 1

    with tenant_context(tenant_a):
        async with session_scope() as session:
            echo_count = await session.scalar(
                select(func.count())
                .select_from(TenantIsolationCanary)
                .where(TenantIsolationCanary.note == f"echo:{canary_id}")
            )
    assert echo_count == 1


# ---------------------------------------------------------------------------
# RLS на outbox: тенант видит только свои события, диспетчер — все
# ---------------------------------------------------------------------------


async def test_outbox_rows_are_tenant_isolated(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await _publish_note(tenant_a, "a-event")
    await _publish_note(tenant_b, "b-event")

    with tenant_context(tenant_a):
        async with session_scope() as session:
            visible = (await session.execute(select(OutboxEvent.payload))).scalars().all()
    assert [payload["note"] for payload in visible] == ["a-event"]

    # Платформенная сессия (диспетчер воркера) обязана видеть очередь целиком —
    # политика platform_dispatch (миграция 0003), осознанное исключение канона.
    async with platform_session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert total == 2
