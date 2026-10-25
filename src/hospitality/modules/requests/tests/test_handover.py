"""Передача заявки (spec 0042 §6 пп. 1–4, issue #216): возврат в очередь
`in_progress → new` и передача в другую службу — пары `claimed_*`, события,
ошибки с кодами каталога, изоляция тенантов и строки журнала изменений.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_NOT_FOUND,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_TRANSFER_REJECTED,
    STATUS_TRANSITIONS,
    ActingUser,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    create_request,
    get_request,
    transfer_request,
)
from hospitality.modules.requests.tests.conftest import (
    make_acting_user,
    make_category,
    shift_request,
)
from hospitality.platform.models import StaffAction, StaffActionLog
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context


async def _request(category_id: uuid.UUID) -> ServiceRequestRead:
    return await create_request(
        ServiceRequestCreate(
            category_id=category_id,
            origin=ServiceRequestOrigin.GUEST_CHAT,
            summary="убрать 305",
            room_number="305",
        )
    )


async def _journal() -> list[StaffActionLog]:
    """Строки журнала текущего тенанта (вызывается внутри `tenant_context`)."""
    async with session_scope() as session:
        return list(
            await session.scalars(select(StaffActionLog).order_by(StaffActionLog.occurred_at))
        )


async def _outbox(request_id: uuid.UUID) -> list[tuple[str, dict[str, object]]]:
    async with platform_session_scope() as session:
        rows = await session.scalars(select(OutboxEvent).order_by(OutboxEvent.occurred_at))
        return [
            (row.event_name, row.payload)
            for row in rows
            if row.payload.get("request_id") == str(request_id)
        ]


async def _actor(name: str) -> ActingUser:
    actor = await make_acting_user(name)
    return actor.model_copy(update={"login": name[:4].upper()})


async def test_requeue_clears_claim_pair_and_keeps_created_at(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.1: возврат снимает время и имя взятия разом; возраст заявки — от
    создания; следующее «Взять» ставит свежие время и имя."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    maid = await _actor("Горничная")
    senior = await _actor("Старшая")

    with tenant_context(tenant_a):
        request = await _request(category.id)
        await change_request_status(request.id, RequestStatus.IN_PROGRESS, acting_user=maid)
        await shift_request(request.id, claimed_at=utc_now() - timedelta(hours=3))

        returned = await change_request_status(request.id, RequestStatus.NEW, acting_user=senior)
        assert returned.status is RequestStatus.NEW
        assert returned.claimed_at is None
        assert returned.claimed_by_user_id is None
        assert returned.claimed_by_display_name is None
        assert returned.created_at == request.created_at

        reclaimed = await change_request_status(
            request.id, RequestStatus.IN_PROGRESS, acting_user=senior
        )
    assert reclaimed.claimed_by_display_name == "Старшая"
    assert reclaimed.claimed_at is not None
    assert reclaimed.claimed_at > utc_now() - timedelta(minutes=1)


@pytest.mark.parametrize(
    "status_path",
    [
        (),  # new → new
        (RequestStatus.IN_PROGRESS, RequestStatus.DONE),
        (RequestStatus.CANCELLED,),
    ],
)
async def test_requeue_only_from_in_progress(
    two_tenants: tuple[uuid.UUID, uuid.UUID], status_path: tuple[RequestStatus, ...]
) -> None:
    """§6 п. 1: вернуть можно только взятую; закрытую — нельзя (#55 — отдельно)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await _request(category.id)
        for status in status_path:
            await change_request_status(request.id, status)
        with pytest.raises(AppError) as error:
            await change_request_status(request.id, RequestStatus.NEW)
    assert error.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
    assert error.value.status_code == 409


async def test_transfer_new_request_keeps_status_and_publishes_event(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.2: невзятая заявка меняет службу и остаётся `new`; событие одно."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    engineering = await make_category(tenant_a, key="engineering", name="Инженер")

    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)
        moved = await transfer_request(request.id, engineering.id)
        stored = await get_request(request.id)

    assert (moved.category_id, moved.status) == (engineering.id, RequestStatus.NEW)
    assert stored == moved
    events = await _outbox(request.id)
    assert [name for name, _ in events] == ["request.created", "request.category_changed"]
    assert events[1][1] == {
        "request_id": str(request.id),
        "old_category_id": str(housekeeping.id),
        "new_category_id": str(engineering.id),
    }


async def test_transfer_claimed_request_returns_it_to_queue(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.2: взятая уходит в очередь новой службы без имени и времени взятия;
    к смене службы добавляется `request.status_changed` (in_progress → new)."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    engineering = await make_category(tenant_a, key="engineering", name="Инженер")

    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)
        await change_request_status(
            request.id, RequestStatus.IN_PROGRESS, acting_user=await _actor("Горничная")
        )
        moved = await transfer_request(request.id, engineering.id)

    assert (moved.category_id, moved.status) == (engineering.id, RequestStatus.NEW)
    assert (moved.claimed_at, moved.claimed_by_user_id, moved.claimed_by_display_name) == (
        None,
        None,
        None,
    )
    events = await _outbox(request.id)
    assert [name for name, _ in events][-2:] == [
        "request.category_changed",
        "request.status_changed",
    ]
    assert events[-1][1]["old_status"] == "in_progress"
    assert events[-1][1]["new_status"] == "new"
    assert events[-1][1]["initiator"] == "staff"


@pytest.mark.parametrize(
    "status_path",
    [(RequestStatus.IN_PROGRESS, RequestStatus.DONE), (RequestStatus.CANCELLED,)],
)
async def test_transfer_of_closed_request_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID], status_path: tuple[RequestStatus, ...]
) -> None:
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    engineering = await make_category(tenant_a, key="engineering", name="Инженер")

    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)
        for status in status_path:
            await change_request_status(request.id, status)
        with pytest.raises(AppError) as error:
            await transfer_request(request.id, engineering.id, acting_user=await _actor("Анна"))
        stored = await get_request(request.id)
        journal = await _journal()

    assert error.value.code == ERR_REQUESTS_TRANSFER_REJECTED
    assert error.value.status_code == 409
    assert stored.category_id == housekeeping.id
    assert journal == []


async def test_transfer_to_same_category_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Та же служба — гонка двух передавших: второй получает 409, а не тихий no-op."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)
        with pytest.raises(AppError) as error:
            await transfer_request(request.id, housekeeping.id)
    assert error.value.code == ERR_REQUESTS_TRANSFER_REJECTED
    assert [name for name, _ in await _outbox(request.id)] == ["request.created"]


async def test_transfer_to_other_tenants_category_is_not_found(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§6 п. 3 (P-4): служба другого отеля неотличима от несуществующей."""
    tenant_a, tenant_b = two_tenants
    housekeeping = await make_category(tenant_a)
    foreign = await make_category(tenant_b, key="engineering", name="Инженер")

    with tenant_context(tenant_a):
        request = await _request(housekeeping.id)
        for category_id in (foreign.id, uuid.uuid4()):
            with pytest.raises(AppError) as error:
                await transfer_request(request.id, category_id)
            assert error.value.code == ERR_REQUESTS_CATEGORY_NOT_FOUND
            assert error.value.status_code == 404
        assert (await get_request(request.id)).category_id == housekeeping.id


async def test_journal_rows_for_requeue_and_transfer(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.3: возврат — строка «возвращена», передача взятой — ОДНА строка
    «передана» с откуда/куда; без личности строк нет."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    engineering = await make_category(tenant_a, key="engineering", name="Инженер")
    senior = await _actor("Старшая")

    with tenant_context(tenant_a):
        returned = await _request(housekeeping.id)
        await change_request_status(returned.id, RequestStatus.IN_PROGRESS)
        await change_request_status(returned.id, RequestStatus.NEW, acting_user=senior)

        transferred = await _request(housekeeping.id)
        await change_request_status(transferred.id, RequestStatus.IN_PROGRESS)
        await transfer_request(transferred.id, engineering.id, acting_user=senior)

        silent = await _request(housekeeping.id)
        await change_request_status(silent.id, RequestStatus.IN_PROGRESS)
        await change_request_status(silent.id, RequestStatus.NEW)
        await transfer_request(silent.id, engineering.id)

        rows = await _journal()

    assert [(row.action, row.request_id) for row in rows] == [
        (StaffAction.REQUEST_RETURNED, returned.id),
        (StaffAction.REQUEST_TRANSFERRED, transferred.id),
    ]
    back, moved = rows
    assert (back.category_name, back.category_name_to, back.room_number) == (
        "Housekeeping",
        None,
        "305",
    )
    assert (moved.category_name, moved.category_name_to) == ("Housekeeping", "Инженер")
    # Служба строки — id откуда / куда: по ним старший видит строки своих служб
    # (spec 0045 §3). «Откуда» — прежняя служба, хотя заявка уже в новой.
    assert (back.category_id, back.category_id_to) == (housekeeping.id, None)
    assert (moved.category_id, moved.category_id_to) == (housekeeping.id, engineering.id)
    assert moved.request_daily_number == transferred.daily_number
    assert {row.actor_login for row in rows} == {"СТАР"}


async def test_every_transition_with_actor_writes_one_journal_row(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Каждая цель `STATUS_TRANSITIONS` есть в карте действий журнала: новое
    ребро без строки в карте дало бы KeyError и 500 на действии кабинета."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    actor = await _actor("Анна")
    reach = {RequestStatus.NEW: (), RequestStatus.IN_PROGRESS: (RequestStatus.IN_PROGRESS,)}

    with tenant_context(tenant_a):
        for source, targets in STATUS_TRANSITIONS.items():
            if not targets:
                continue
            assert source in reach, f"нет пути к открытому статусу {source}"
            for target in targets:
                request = await _request(category.id)
                for status in reach[source]:
                    await change_request_status(request.id, status)
                before = len(await _journal())
                await change_request_status(request.id, target, acting_user=actor)
                assert len(await _journal()) == before + 1, (source, target)
