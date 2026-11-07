"""Гость напоминает о заявке — счёт в домене (spec 0070 §3, §8; issue #834)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from hospitality.modules.requests import reminders
from hospitality.modules.requests.api import (
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    change_request_status,
    create_request,
    get_request,
    list_guest_reminded_requests,
    record_guest_reminder,
)
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context


async def _request(category_id: uuid.UUID, summary: str = "полотенца") -> uuid.UUID:
    created = await create_request(
        ServiceRequestCreate(
            origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category_id, summary=summary
        )
    )
    return created.id


async def test_counts_and_stamps_application_time(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каждое напоминание — +1 и время последнего; время — часы приложения
    (`utc_now`), с которыми лестница сравнивает `created_at`."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    first_at = utc_now() + timedelta(minutes=40)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        fresh = await get_request(request_id)
        assert (fresh.guest_reminder_count, fresh.guest_reminded_at) == (0, None)

        monkeypatch.setattr(reminders, "utc_now", lambda: first_at)
        first = await record_guest_reminder(request_id)
        second_at = first_at + timedelta(minutes=5)
        monkeypatch.setattr(reminders, "utc_now", lambda: second_at)
        second = await record_guest_reminder(request_id)

    assert first is not None and second is not None
    assert (first.guest_reminder_count, first.guest_reminded_at) == (1, first_at)
    assert (second.guest_reminder_count, second.guest_reminded_at) == (2, second_at)
    assert second.status is RequestStatus.NEW


async def test_in_progress_counts_closed_and_unknown_do_not(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        taken = await _request(category.id)
        await change_request_status(taken, RequestStatus.IN_PROGRESS)
        done = await _request(category.id)
        await change_request_status(done, RequestStatus.IN_PROGRESS)
        await change_request_status(done, RequestStatus.DONE)

        recorded = await record_guest_reminder(taken)
        assert recorded is not None and recorded.guest_reminder_count == 1
        assert await record_guest_reminder(done) is None
        assert await record_guest_reminder(uuid.uuid4()) is None
        assert (await get_request(done)).guest_reminder_count == 0


async def test_two_reminders_at_once_both_count(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Атомарный UPDATE: «где? где?» двумя сообщениями разом — два счёта, не один."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        await asyncio.gather(*(record_guest_reminder(request_id) for _ in range(2)))
        assert (await get_request(request_id)).guest_reminder_count == 2


async def test_other_tenant_cannot_remind(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """RLS: id чужой заявки из контекста другого отеля — как несуществующей."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
    with tenant_context(tenant_b):
        assert await record_guest_reminder(request_id) is None
        assert await list_guest_reminded_requests(limit=10) == []
    with tenant_context(tenant_a):
        assert (await get_request(request_id)).guest_reminder_count == 0


async def test_reminded_list_open_only_fresh_first_and_sliced_by_service(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    towels_category = await make_category(tenant_a)
    pipe_category = await make_category(tenant_a, key="maintenance", name="Maintenance")
    start = utc_now()
    with tenant_context(tenant_a):
        towels = await _request(towels_category.id)
        pipe = await _request(pipe_category.id, "течёт кран")
        silent = await _request(towels_category.id, "без напоминаний")
        closed = await _request(towels_category.id, "закрыли")
        for minutes, request_id in ((1, towels), (2, pipe), (3, closed)):
            monkeypatch.setattr(
                reminders, "utc_now", lambda m=minutes: start + timedelta(minutes=m)
            )
            await record_guest_reminder(request_id)
        await change_request_status(closed, RequestStatus.CANCELLED)

        everything = await list_guest_reminded_requests(limit=10)
        housekeeping = await list_guest_reminded_requests(
            limit=10, category_ids=[towels_category.id]
        )
        nothing = await list_guest_reminded_requests(limit=10, category_ids=[])

    assert [request.id for request in everything] == [pipe, towels]
    assert [request.id for request in housekeeping] == [towels]
    assert silent not in {request.id for request in everything}
    assert nothing == []
