"""Причина отмены заявки (spec 0057 §2): пишется только переходом в `cancelled`,
у отмены гостем — всегда `by_guest`, у незакрытой заявки её не держит и БД."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from hospitality.modules.requests.api import (
    CancelReason,
    RequestInitiator,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    change_request_status,
    create_request,
    get_request,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context


async def _new_request(tenant_id: uuid.UUID) -> uuid.UUID:
    category = await make_category(tenant_id)
    with tenant_context(tenant_id):
        request = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="towels"
            )
        )
    return request.id


async def test_reason_is_written_on_cancel(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    request_id = await _new_request(tenant_a)
    with tenant_context(tenant_a):
        cancelled = await change_request_status(
            request_id,
            RequestStatus.CANCELLED,
            cancel_reason=CancelReason.UNAVAILABLE,
            initiator=RequestInitiator.STAFF,
        )
        stored = await get_request(request_id)
    assert cancelled.cancel_reason is CancelReason.UNAVAILABLE
    assert stored.cancel_reason is CancelReason.UNAVAILABLE


async def test_reason_is_ignored_on_other_transitions(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Готово» с причиной отмены — причина не пишется (иначе CHECK бы упал)."""
    tenant_a, _ = two_tenants
    request_id = await _new_request(tenant_a)
    with tenant_context(tenant_a):
        await change_request_status(
            request_id, RequestStatus.IN_PROGRESS, cancel_reason=CancelReason.DUPLICATE
        )
        done = await change_request_status(
            request_id, RequestStatus.DONE, cancel_reason=CancelReason.DUPLICATE
        )
    assert done.status is RequestStatus.DONE
    assert done.cancel_reason is None


@pytest.mark.parametrize("passed", [None, CancelReason.DUPLICATE])
async def test_guest_cancel_is_always_by_guest(
    two_tenants: tuple[uuid.UUID, uuid.UUID], passed: CancelReason | None
) -> None:
    tenant_a, _ = two_tenants
    request_id = await _new_request(tenant_a)
    with tenant_context(tenant_a):
        cancelled = await change_request_status(
            request_id,
            RequestStatus.CANCELLED,
            cancel_reason=passed,
            initiator=RequestInitiator.GUEST,
        )
    assert cancelled.cancel_reason is CancelReason.BY_GUEST


async def test_cancel_without_reason_stays_empty(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Пути без причины (Telegram до PR 2, HTTP API, заказ в номер) — пустая колонка."""
    tenant_a, _ = two_tenants
    request_id = await _new_request(tenant_a)
    with tenant_context(tenant_a):
        cancelled = await change_request_status(request_id, RequestStatus.CANCELLED)
    assert cancelled.cancel_reason is None


async def test_database_rejects_reason_on_open_request(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    request_id = await _new_request(tenant_a)
    with tenant_context(tenant_a), pytest.raises(IntegrityError, match="cancel_reason"):
        async with session_scope() as session:
            await session.execute(
                update(ServiceRequest)
                .where(ServiceRequest.id == request_id)
                .values(cancel_reason=CancelReason.DUPLICATE)
            )
