"""События модуля requests (Task 0012, P-6): `request.created` и
`request.status_changed` публикуются в outbox атомарно с бизнес-записью
и доставляются подписчику; отвергнутые операции не публикуют ничего.

Механика конвейера (ретраи, backoff, идемпотентность повторной доставки)
покрыта канонными тестами Task 0010 (`tests/test_events.py`) и здесь
не дублируется.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from hospitality.modules.requests.api import (
    RequestCreated,
    RequestStatus,
    RequestStatusChanged,
    ServiceRequestCreate,
    change_request_status,
    create_request,
)
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent, deliver_pending_events, subscribe
from hospitality.shared.tenancy import current_tenant_id, tenant_context


async def _outbox_rows(event_name: str) -> list[OutboxEvent]:
    async with platform_session_scope() as session:
        rows = await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.event_name == event_name)
            .order_by(OutboxEvent.occurred_at)
        )
    return list(rows.scalars().all())


async def test_create_request_publishes_request_created(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="Extra towels, room 310")
        )

    (row,) = await _outbox_rows("request.created")
    assert row.tenant_id == tenant_a
    assert row.processed_at is None
    assert row.payload == {
        "request_id": str(request.id),
        "category_id": str(category.id),
        "summary": "Extra towels, room 310",
    }


async def test_status_change_publishes_request_status_changed(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="Fix AC")
        )
        await change_request_status(request.id, RequestStatus.ASSIGNED)
        await change_request_status(request.id, RequestStatus.CANCELLED)

    rows = await _outbox_rows("request.status_changed")
    assert [row.payload for row in rows] == [
        {"request_id": str(request.id), "old_status": "new", "new_status": "assigned"},
        {"request_id": str(request.id), "old_status": "assigned", "new_status": "cancelled"},
    ]


async def test_events_are_delivered_to_subscriber_in_tenant_context(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сквозная проверка контракта: события модуля проходят конвейер Task 0010
    и приходят подписчику типизированными, в контексте тенанта события."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    seen: list[tuple[str, uuid.UUID]] = []

    async def on_created(event: RequestCreated) -> None:
        seen.append((event.summary, current_tenant_id()))

    async def on_status_changed(event: RequestStatusChanged) -> None:
        seen.append((f"{event.old_status.value}->{event.new_status.value}", current_tenant_id()))

    subscribe(RequestCreated, on_created)
    subscribe(RequestStatusChanged, on_status_changed)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="Late checkout")
        )
        await change_request_status(request.id, RequestStatus.ASSIGNED)

    assert await deliver_pending_events() == 2
    assert seen == [("Late checkout", tenant_a), ("new->assigned", tenant_a)]


async def test_rejected_operations_publish_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Атомарность P-6 с обратной стороны: откат бизнес-операции не оставляет
    события — ни при неизвестной категории, ни при недопустимом переходе."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        with pytest.raises(AppError):
            await create_request(ServiceRequestCreate(category_id=uuid.uuid4(), summary="ghost"))
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="real")
        )
        with pytest.raises(AppError):
            await change_request_status(request.id, RequestStatus.DONE)  # new → done запрещён

    async with platform_session_scope() as session:
        created_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_name == "request.created")
        )
        status_changed_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_name == "request.status_changed")
        )
    assert created_count == 1  # только успешная заявка
    assert status_changed_count == 0
