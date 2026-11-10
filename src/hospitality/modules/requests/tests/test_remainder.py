"""«Готово» с остатком (spec 0079 §2.1, issue #58).

Исходная закрывается `done` с примечанием, остаток — новая заявка `new` в той же
службе со ссылкой на исходную; обе записи и оба события — одной транзакцией, а
отказ перехода не оставляет остатка.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests import service
from hospitality.modules.requests.api import (
    ERR_REQUESTS_FOREIGN_SERVICE,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    complete_with_remainder,
    create_request,
    get_request,
    list_open_requests,
)
from hospitality.modules.requests.tests.conftest import make_acting_user, make_category
from hospitality.platform.models import StaffAction, StaffActionLog
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context

_NOTE = "нет стеклоочистителя, окно не помыто"


async def _claimed(tenant_id: uuid.UUID, *, is_urgent: bool = False) -> ServiceRequestRead:
    category = await make_category(tenant_id)
    stay_id = uuid.uuid4()
    with tenant_context(tenant_id):
        created = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="уборка номера",
                room_number="704",
                guest_language="kk",
                is_urgent=is_urgent,
            ),
            stay_id=stay_id,
        )
        return await change_request_status(created.id, RequestStatus.IN_PROGRESS)


async def _outbox_names() -> list[str]:
    async with platform_session_scope() as session:
        rows = await session.scalars(select(OutboxEvent).order_by(OutboxEvent.occurred_at))
        return [row.event_name for row in rows]


async def test_remainder_is_a_new_request_of_the_same_service(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    parent = await _claimed(tenant_a, is_urgent=True)

    with tenant_context(tenant_a):
        outcome = await complete_with_remainder(parent.id, resolution_note=f"  {_NOTE} ")
        closed = await get_request(parent.id)
        open_ids = [request.id for request in await list_open_requests(limit=10)]

    remainder = outcome.remainder
    assert closed.status is RequestStatus.DONE
    assert closed.resolution_note == _NOTE
    assert outcome.closed.id == parent.id
    assert remainder.status is RequestStatus.NEW
    assert remainder.origin is ServiceRequestOrigin.REMAINDER
    assert remainder.parent_request_id == parent.id
    assert remainder.category_id == parent.category_id
    assert remainder.room_number == "704"
    assert remainder.stay_id == parent.stay_id
    assert remainder.guest_language == "kk"
    assert remainder.summary == "уборка номера"
    assert remainder.details == (
        f"Остаток заявки #{parent.daily_number} от {parent.service_day:%d.%m}. "
        f"Примечание: {_NOTE}"
    )
    # Срочность исходной разобрана — остаток обычный; оплата и квота не наследуются.
    assert remainder.is_urgent is False
    assert remainder.payment_required is False
    assert remainder.quota_key is None
    assert remainder.daily_number == (parent.daily_number or 0) + 1
    assert open_ids == [remainder.id]


async def test_close_and_remainder_publish_both_events(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    parent = await _claimed(tenant_a)
    before = await _outbox_names()

    with tenant_context(tenant_a):
        await complete_with_remainder(parent.id, resolution_note=_NOTE)

    assert (await _outbox_names())[len(before) :] == ["request.status_changed", "request.created"]


async def test_acting_user_gets_close_and_create_journal_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    parent = await _claimed(tenant_a)
    maid = await make_acting_user("Айгуль")

    with tenant_context(tenant_a):
        outcome = await complete_with_remainder(
            parent.id, resolution_note=_NOTE, acting_user=maid
        )
        async with session_scope() as session:
            rows = list(
                await session.scalars(
                    select(StaffActionLog).order_by(StaffActionLog.occurred_at)
                )
            )

    assert [(row.action, row.request_id, row.with_note) for row in rows] == [
        (StaffAction.REQUEST_DONE, parent.id, True),
        (StaffAction.REQUEST_CREATED, outcome.remainder.id, False),
    ]
    assert outcome.closed.closed_by_display_name == "Айгуль"


async def test_empty_note_is_rejected_before_any_write(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    parent = await _claimed(tenant_a)

    with tenant_context(tenant_a), pytest.raises(ValueError):
        await complete_with_remainder(parent.id, resolution_note="   ")
    with tenant_context(tenant_a):
        assert (await get_request(parent.id)).status is RequestStatus.IN_PROGRESS


async def test_rejected_transition_leaves_no_remainder(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    parent = await _claimed(tenant_a)
    other = await make_category(tenant_a, key="engineering", name="Инженерия")

    with tenant_context(tenant_a):
        with pytest.raises(AppError) as foreign:
            await complete_with_remainder(
                parent.id, resolution_note=_NOTE, allowed_category_ids={other.id}
            )
        await change_request_status(parent.id, RequestStatus.DONE)
        with pytest.raises(AppError) as closed:
            await complete_with_remainder(parent.id, resolution_note=_NOTE)
        open_requests = await list_open_requests(limit=10)

    assert foreign.value.code == ERR_REQUESTS_FOREIGN_SERVICE
    assert closed.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
    assert open_requests == []


async def test_daily_number_collision_retries_whole_transaction(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, _ = two_tenants
    parent = await _claimed(tenant_a)
    original_next = service._next_daily_number
    calls = {"n": 0}

    async def stale_then_real(session: AsyncSession, service_day: date) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return parent.daily_number or 1  # номер уже занят исходной
        return await original_next(session, service_day)

    monkeypatch.setattr(service, "_next_daily_number", stale_then_real)

    with tenant_context(tenant_a):
        outcome = await complete_with_remainder(parent.id, resolution_note=_NOTE)
        open_requests = await list_open_requests(limit=10)

    assert calls["n"] == 2
    assert outcome.closed.status is RequestStatus.DONE
    assert [request.id for request in open_requests] == [outcome.remainder.id]
