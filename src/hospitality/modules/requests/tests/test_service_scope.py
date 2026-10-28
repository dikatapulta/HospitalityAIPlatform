"""Граница службы в домене (spec 0045 §5, §10 «PR 2»; #221 п. 2).

Ленты режут по службам и «моим» в SQL до среза `limit`; действие над заявкой
службы не из `allowed_category_ids` — 403 `ERR-REQUESTS-007`, заявка, журнал и
outbox не меняются. Проверка службы стоит под той же блокировкой строки, что
переход: передача, закоммиченная, пока действие ждало строку, даёт 403.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import (
    ERR_REQUESTS_FOREIGN_SERVICE,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    create_request,
    get_request,
    list_open_requests,
    list_requests_closed_since,
    list_unclaimed_requests,
    transfer_request,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.tests.conftest import (
    make_acting_user,
    make_category,
    shift_request,
)
from hospitality.modules.requests.tests.test_handover import _journal, _outbox
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.test_tenant_config import _wait_until_a_backend_blocks


async def _request(category_id: uuid.UUID, summary: str = "убрать 305") -> ServiceRequestRead:
    return await create_request(
        ServiceRequestCreate(
            category_id=category_id,
            origin=ServiceRequestOrigin.GUEST_CHAT,
            summary=summary,
            room_number="305",
        )
    )


async def test_open_feed_is_cut_by_service_in_sql_before_limit(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Три свежие заявки уборки и две старые — техники: с потолком 2 техник
    видит обе свои. Фильтр в памяти после среза дал бы ему пустую ленту (#221)."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    with tenant_context(tenant_a):
        old = [await _request(maintenance.id, f"труба {n}") for n in range(2)]
        for n, request in enumerate(old):
            await shift_request(request.id, created_at=utc_now() - timedelta(hours=2 + n))
        for n in range(3):
            await _request(housekeeping.id, f"полотенца {n}")

        mine = await list_open_requests(limit=2, category_ids={maintenance.id})
        nothing = await list_open_requests(limit=2, category_ids=set())
        everything = await list_open_requests(limit=10)

    assert {request.id for request in mine} == {request.id for request in old}
    assert nothing == []
    assert len(everything) == 5


async def test_mine_is_filtered_in_sql_before_limit(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Мои» — тоже в SQL: старая взятая мною заявка видна при потолке 1, хотя
    сверху лежат более новые чужие."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    me = await make_acting_user("Айгуль")
    with tenant_context(tenant_a):
        taken = await _request(category.id, "старая")
        await change_request_status(taken.id, RequestStatus.IN_PROGRESS, acting_user=me)
        await shift_request(taken.id, created_at=utc_now() - timedelta(hours=3))
        await _request(category.id, "новая")

        open_mine = await list_open_requests(limit=1, claimed_by_user_id=me.user_id)
        await change_request_status(taken.id, RequestStatus.DONE, acting_user=me)
        closed_mine = await list_requests_closed_since(
            closed_after=utc_now() - timedelta(hours=1),
            limit=1,
            claimed_by_user_id=me.user_id,
        )

    assert [request.id for request in open_mine] == [taken.id]
    assert [request.id for request in closed_mine] == [taken.id]


async def test_closed_and_unclaimed_feeds_are_cut_by_service(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    with tenant_context(tenant_a):
        towels = await _request(housekeeping.id)
        pipe = await _request(maintenance.id)
        closed_towels = await _request(housekeeping.id, "закрыта")
        closed_pipe = await _request(maintenance.id, "закрыта")
        for request in (closed_towels, closed_pipe):
            await change_request_status(request.id, RequestStatus.CANCELLED)
        since = utc_now() - timedelta(hours=1)

        unclaimed = await list_unclaimed_requests(
            created_before=utc_now() + timedelta(seconds=1),
            limit=10,
            category_ids={housekeeping.id},
        )
        closed = await list_requests_closed_since(
            closed_after=since, limit=10, category_ids={maintenance.id}
        )

    assert [request.id for request in unclaimed] == [towels.id]
    assert [request.id for request in closed] == [closed_pipe.id]
    assert pipe.id not in {request.id for request in unclaimed}


@pytest.mark.parametrize(
    ("claimed_first", "target"),
    [
        (False, RequestStatus.IN_PROGRESS),
        (True, RequestStatus.DONE),
        (False, RequestStatus.CANCELLED),
        (True, RequestStatus.NEW),
    ],
)
async def test_status_change_on_foreign_service_is_403_and_changes_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    claimed_first: bool,
    target: RequestStatus,
) -> None:
    """Взять, готово, отменить, вернуть в очередь — заявку техники сотрудник
    уборки не трогает: 403, статус, журнал и outbox прежние."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    maid = await make_acting_user("Горничная")
    with tenant_context(tenant_a):
        pipe = await _request(maintenance.id)
        if claimed_first:
            await change_request_status(pipe.id, RequestStatus.IN_PROGRESS)
        before = await get_request(pipe.id)
        events_before = await _outbox(pipe.id)

        with pytest.raises(AppError) as error:
            await change_request_status(
                pipe.id,
                target,
                resolution_note="готово" if target is RequestStatus.DONE else None,
                acting_user=maid,
                allowed_category_ids={housekeeping.id},
            )
        after = await get_request(pipe.id)
        journal = await _journal()

    assert error.value.code == ERR_REQUESTS_FOREIGN_SERVICE
    assert error.value.status_code == 403
    assert after == before
    assert journal == []
    assert await _outbox(pipe.id) == events_before


async def test_own_service_action_passes_and_transfer_goes_anywhere(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Своя заявка — как раньше; передать её можно в любую службу (§3), а
    передать чужую — нельзя."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    maid = await make_acting_user("Горничная")
    with tenant_context(tenant_a):
        towels = await _request(housekeeping.id)
        claimed = await change_request_status(
            towels.id,
            RequestStatus.IN_PROGRESS,
            acting_user=maid,
            allowed_category_ids={housekeeping.id},
        )
        moved = await transfer_request(
            towels.id, maintenance.id, acting_user=maid, allowed_category_ids={housekeeping.id}
        )
        with pytest.raises(AppError) as error:
            await transfer_request(
                towels.id,
                housekeeping.id,
                acting_user=maid,
                allowed_category_ids={housekeeping.id},
            )

    assert claimed.status is RequestStatus.IN_PROGRESS
    assert moved.category_id == maintenance.id
    assert error.value.code == ERR_REQUESTS_FOREIGN_SERVICE


async def test_action_after_transfer_is_403(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Горничная видела заявку у себя, её передали в технику — «Готово» прежней
    службы уже не проходит."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)
        await change_request_status(request.id, RequestStatus.IN_PROGRESS)
        await transfer_request(request.id, maintenance.id)
        with pytest.raises(AppError) as error:
            await change_request_status(
                request.id, RequestStatus.DONE, allowed_category_ids={housekeeping.id}
            )
        after = await get_request(request.id)

    assert error.value.code == ERR_REQUESTS_FOREIGN_SERVICE
    assert after.status is RequestStatus.NEW
    assert after.category_id == maintenance.id


async def test_service_is_checked_under_the_row_lock(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Передача держит строку и ещё не закоммичена, «Взять» прежней службы уже
    ждёт эту строку. После коммита передачи «Взять» видит новую службу — 403.
    Проверка до блокировки прочла бы старую службу и взяла бы чужую заявку.
    """
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    maintenance = await make_category(tenant_a, key="maintenance", name="Maintenance")
    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)

        async def claim_by_old_service() -> None:
            await change_request_status(
                request.id, RequestStatus.IN_PROGRESS, allowed_category_ids={housekeeping.id}
            )

        async with session_scope() as transfer_session:
            row = (
                await transfer_session.execute(
                    select(ServiceRequest).where(ServiceRequest.id == request.id).with_for_update()
                )
            ).scalar_one()
            row.category_id = maintenance.id
            await transfer_session.flush()
            claim = asyncio.create_task(claim_by_old_service())
            blocked = await _wait_until_a_backend_blocks()
        # Выход из scope — коммит передачи: «Взять» просыпается здесь.
        with pytest.raises(AppError) as error:
            await claim
        after = await get_request(request.id)

    assert blocked, "«Взять» не встало на блокировку — чередования не было"
    assert error.value.code == ERR_REQUESTS_FOREIGN_SERVICE
    assert after.status is RequestStatus.NEW
