"""Срез лент по этажам и «Направить» в домене (spec 0053 §2, §4а; issue #856).

Правило этажа — одно в двух формах (`shared/rooms.py`): SQL-форма обязана
давать тот же этаж, что Python-форма, на одном наборе номеров. Срез прячет
заявку служб среза с чужого этажа до `limit`, но пропускает взятую или
направленную самому смотрящему, срочную, эскалацию и номер без этажа.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import String, literal, select

from hospitality.modules.requests.api import (
    ERR_REQUESTS_FOREIGN_SERVICE,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    FloorScope,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    create_request,
    direct_request,
    get_request,
    list_open_requests,
    list_unclaimed_requests,
    transfer_request,
)
from hospitality.modules.requests.tests.conftest import (
    make_acting_user,
    make_category,
    shift_request,
)
from hospitality.modules.requests.tests.test_handover import _journal
from hospitality.platform.models import StaffAction
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.rooms import floor_of_room, floor_of_room_sql
from hospitality.shared.tenancy import tenant_context

ROOMS = [
    "704",
    " 704 ",
    "1204",
    "2601",
    "9999",
    "0704",
    "100",
    "099",
    "050",
    "12",
    "12345",
    "Люкс",
    "A-704",
    "704а",
    "７０４",
    "",
    None,
]


async def test_sql_floor_matches_python_floor(canonical_database: None) -> None:
    """P-12: правило этажа в базе и в Python — одно; правишь одно — правь другое."""
    async with platform_session_scope() as session:
        for room in ROOMS:
            sql_floor = await session.scalar(
                select(floor_of_room_sql(literal(room, type_=String())))
            )
            assert sql_floor == floor_of_room(room), room


async def _request(
    category_id: uuid.UUID,
    room: str,
    *,
    is_urgent: bool = False,
    origin: ServiceRequestOrigin = ServiceRequestOrigin.GUEST_CHAT,
) -> ServiceRequestRead:
    return await create_request(
        ServiceRequestCreate(
            category_id=category_id,
            origin=origin,
            summary=f"полотенца {room}",
            room_number=room,
            is_urgent=is_urgent,
        )
    )


async def test_floor_scope_hides_other_floors_but_lets_exceptions_through(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Горничная 7-го при команде на 8-м: 805 не видна; 704, «Люкс», срочная и
    эскалация с 8-го, взятая ею и направленная ей 8xx — видны; заявка другой
    службы из 805 срез не трогает."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    me = await make_acting_user("Айгуль")
    with tenant_context(tenant_a):
        hidden = await _request(housekeeping.id, "805")
        visible = [
            await _request(housekeeping.id, "704"),
            await _request(housekeeping.id, "Люкс"),
            await _request(housekeeping.id, "806", is_urgent=True),
            await _request(housekeeping.id, "807", origin=ServiceRequestOrigin.ESCALATION),
            await _request(maintenance.id, "805"),
        ]
        taken = await _request(housekeeping.id, "808")
        await change_request_status(taken.id, RequestStatus.IN_PROGRESS, acting_user=me)
        directed = await _request(housekeeping.id, "809")
        await direct_request(
            directed.id, to_user_id=me.user_id, to_display_name="Айгуль", acting_user=me
        )
        scope = FloorScope(
            category_ids=frozenset({housekeeping.id}),
            hidden_floors=frozenset({8}),
            user_id=me.user_id,
        )

        seen = {request.id for request in await list_open_requests(limit=50, floor_scope=scope)}

    assert hidden.id not in seen
    assert seen == {request.id for request in visible} | {taken.id, directed.id}


async def test_floor_scope_cuts_in_sql_before_limit(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Три свежие заявки с чужого этажа сверху, своя — старая: при потолке 1
    сигнал видит свою. Срез в памяти после `limit` дал бы пустой ответ (#221)."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    me = await make_acting_user("Айгуль")
    with tenant_context(tenant_a):
        mine = await _request(housekeeping.id, "704")
        await shift_request(mine.id, created_at=utc_now() - timedelta(hours=1))
        for room in ("805", "806", "807"):
            await _request(housekeeping.id, room)
        scope = FloorScope(
            category_ids=frozenset({housekeeping.id}),
            hidden_floors=frozenset({8}),
            user_id=me.user_id,
        )

        unclaimed = await list_unclaimed_requests(
            created_before=utc_now(), limit=1, floor_scope=scope
        )

    assert [request.id for request in unclaimed] == [mine.id]


async def test_direct_request_names_the_maid_and_writes_journal(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    senior = await make_acting_user("Сауле")
    maid = await make_acting_user("Айгуль")
    with tenant_context(tenant_a):
        request = await _request(housekeeping.id, "1605")

        updated = await direct_request(
            request.id,
            to_user_id=maid.user_id,
            to_display_name="Айгуль",
            acting_user=senior,
            allowed_category_ids={housekeeping.id},
        )
        journal = await _journal()

    assert updated.status is RequestStatus.NEW
    assert (updated.directed_to_user_id, updated.directed_to_display_name) == (
        maid.user_id,
        "Айгуль",
    )
    assert [row.action for row in journal] == [StaffAction.REQUEST_DIRECTED]
    assert journal[0].actor_user_id == senior.user_id
    assert journal[0].room_number == "1605"


async def test_direct_request_refuses_taken_and_foreign_service(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Взятую — 409 ERR-REQUESTS-003; заявку не из служб действующего — 403 007."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    senior = await make_acting_user("Сауле")
    with tenant_context(tenant_a):
        taken = await _request(housekeeping.id, "704")
        await change_request_status(taken.id, RequestStatus.IN_PROGRESS, acting_user=senior)
        foreign = await _request(maintenance.id, "704")

        with pytest.raises(AppError) as taken_error:
            await direct_request(
                taken.id, to_user_id=senior.user_id, to_display_name="Сауле", acting_user=senior
            )
        with pytest.raises(AppError) as foreign_error:
            await direct_request(
                foreign.id,
                to_user_id=senior.user_id,
                to_display_name="Сауле",
                acting_user=senior,
                allowed_category_ids={housekeeping.id},
            )
        foreign_after = await get_request(foreign.id)

    assert (taken_error.value.code, taken_error.value.status_code) == (
        ERR_REQUESTS_INVALID_STATUS_TRANSITION,
        409,
    )
    assert (foreign_error.value.code, foreign_error.value.status_code) == (
        ERR_REQUESTS_FOREIGN_SERVICE,
        403,
    )
    assert foreign_after.directed_to_user_id is None


async def test_transfer_drops_direction(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Направляли горничной — передали технику: в новой службе адресата нет."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    senior = await make_acting_user("Сауле")
    with tenant_context(tenant_a):
        request = await _request(housekeeping.id, "704")
        await direct_request(
            request.id, to_user_id=senior.user_id, to_display_name="Сауле", acting_user=senior
        )

        moved = await transfer_request(request.id, maintenance.id, acting_user=senior)

    assert (moved.directed_to_user_id, moved.directed_to_display_name) == (None, None)
