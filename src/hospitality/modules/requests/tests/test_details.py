"""Дописывание в подробности невзятой заявки (spec 0039 §2.5, #863).

`append_request_details` дописывает только в `new` и только целиком: взятая,
закрытая, чужая заявка или полные подробности — None, и вызывающий заводит новую
заявку вместо того, чтобы просьба гостя пропала.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.requests.api import (
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    append_request_details,
    change_request_status,
    create_request,
    get_request,
)
from hospitality.modules.requests.schemas import REQUEST_DETAILS_MAX_CHARS
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.tenancy import tenant_context


async def _escalation_card(tenant_id: uuid.UUID, details: str | None) -> ServiceRequestRead:
    category = await make_category(tenant_id, key="reception", name="Ресепшен")
    with tenant_context(tenant_id):
        return await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.ESCALATION,
                summary="Гость просит сотрудника: «позовите сотрудника»",
                details=details,
                room_number="2412",
            )
        )


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (None, "Ещё в 10:52: «алло»"),
        (
            "Что бот понял: позвать сотрудника",
            "Что бот понял: позвать сотрудника\nЕщё в 10:52: «алло»",
        ),
    ],
    ids=["empty", "after-existing"],
)
async def test_line_is_appended_to_an_unclaimed_request(
    two_tenants: tuple[uuid.UUID, uuid.UUID], details: str | None, expected: str
) -> None:
    tenant_a, _ = two_tenants
    card = await _escalation_card(tenant_a, details)

    with tenant_context(tenant_a):
        appended = await append_request_details(card.id, "Ещё в 10:52: «алло»")
        stored = await get_request(card.id)

    assert appended is not None
    assert appended.details == stored.details == expected
    assert stored.status is RequestStatus.NEW


@pytest.mark.parametrize(
    "statuses",
    [
        (RequestStatus.IN_PROGRESS,),
        (RequestStatus.IN_PROGRESS, RequestStatus.DONE),
        (RequestStatus.CANCELLED,),
    ],
    ids=["claimed", "done", "cancelled"],
)
async def test_claimed_or_closed_request_is_left_alone(
    two_tenants: tuple[uuid.UUID, uuid.UUID], statuses: tuple[RequestStatus, ...]
) -> None:
    """Взявший уже мог уйти к гостю, не перечитав карточку: дописка туда не дойдёт."""
    tenant_a, _ = two_tenants
    card = await _escalation_card(tenant_a, None)
    with tenant_context(tenant_a):
        for status in statuses:
            await change_request_status(card.id, status)
        assert await append_request_details(card.id, "Ещё в 10:52: «алло»") is None
        assert (await get_request(card.id)).details is None


async def test_line_that_does_not_fit_is_not_cut(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Обрезка съела бы ровно новую просьбу — лучше новая карточка (None)."""
    tenant_a, _ = two_tenants
    full = "x" * (REQUEST_DETAILS_MAX_CHARS - 5)
    card = await _escalation_card(tenant_a, full)

    with tenant_context(tenant_a):
        assert await append_request_details(card.id, "Ещё в 10:52: «алло»") is None
        assert (await get_request(card.id)).details == full


async def test_foreign_or_missing_request_is_not_found(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Чужую заявку RLS не показывает (P-4) — для вызывающего её просто нет."""
    tenant_a, tenant_b = two_tenants
    card = await _escalation_card(tenant_a, None)

    with tenant_context(tenant_b):
        assert await append_request_details(card.id, "Ещё в 10:52: «алло»") is None
        assert await append_request_details(uuid.uuid4(), "Ещё в 10:52: «алло»") is None
    with tenant_context(tenant_a):
        assert (await get_request(card.id)).details is None
