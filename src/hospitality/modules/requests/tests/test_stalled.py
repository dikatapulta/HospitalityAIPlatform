"""`list_stalled_requests` — опора «долго в работе» (issue #120, spec 0028 §8)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import update

from hospitality.modules.requests.api import (
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    change_request_status,
    create_request,
    list_stalled_requests,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context


async def test_only_in_progress_taken_before_cutoff(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Только `in_progress`, взятые раньше границы; без `claimed_at` (строки до
    spec 0035) — по времени создания; чужому тенанту не видны (RLS)."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)
    now = utc_now()

    async def make(summary: str, *, taken: bool = True) -> uuid.UUID:
        request = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary=summary
            )
        )
        if taken:
            await change_request_status(request.id, RequestStatus.IN_PROGRESS)
        return request.id

    async def set_times(request_id: uuid.UUID, **values: object) -> None:
        async with session_scope() as session:
            await session.execute(
                update(ServiceRequest).where(ServiceRequest.id == request_id).values(**values)
            )

    with tenant_context(tenant_a):
        old_claim = await make("взята 5 ч назад")
        await set_times(old_claim, claimed_at=now - timedelta(hours=5))
        recent_claim = await make("взята 2 ч назад")
        await set_times(recent_claim, claimed_at=now - timedelta(hours=2))
        legacy = await make("старая строка без claimed_at")
        await set_times(legacy, claimed_at=None, created_at=now - timedelta(hours=6))
        fresh = await make("взята только что")
        untaken = await make("никто не взял", taken=False)
        await set_times(untaken, created_at=now - timedelta(hours=9))

        stalled = await list_stalled_requests(claimed_before=now - timedelta(hours=1), limit=10)
        ids = [request.id for request in stalled]
        assert ids == [recent_claim, old_claim, legacy]  # недавно взятые сверху
        assert fresh not in ids and untaken not in ids

        limited = await list_stalled_requests(claimed_before=now - timedelta(hours=1), limit=1)
        assert [request.id for request in limited] == [recent_claim]

    with tenant_context(tenant_b):
        assert await list_stalled_requests(claimed_before=now, limit=10) == []
