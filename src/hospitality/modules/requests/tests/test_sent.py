"""«Отправленные» (spec 0063 §2–§3, §7, #700): снимок служб создателя у заявки
и выборка заявок, которые службы отправили другим службам."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.modules.requests.api import (
    ActingUser,
    RequestCategoryRead,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    create_request,
    list_requests_sent_by_services,
    transfer_request,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.tests.conftest import make_acting_user, make_category
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context


async def _actor(*service_keys: str) -> ActingUser:
    user = await make_acting_user("Айгерим")
    return user.model_copy(update={"service_keys": frozenset(service_keys)})


async def _create(
    tenant_id: uuid.UUID,
    category: RequestCategoryRead,
    acting_user: ActingUser | None,
    *,
    summary: str = "течёт кран",
    room_number: str = "704",
) -> ServiceRequestRead:
    with tenant_context(tenant_id):
        return await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=(
                    ServiceRequestOrigin.STAFF_MANUAL
                    if acting_user is not None
                    else ServiceRequestOrigin.GUEST_CHAT
                ),
                summary=summary,
                room_number=room_number,
            ),
            acting_user=acting_user,
        )


async def _snapshot(tenant_id: uuid.UUID, request_id: uuid.UUID) -> list[str]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            row = await session.scalar(
                select(ServiceRequest).where(ServiceRequest.id == request_id)
            )
            assert row is not None
            return row.created_by_service_keys


async def _sent_ids(
    tenant_id: uuid.UUID,
    *service_keys: str,
    room_number: str | None = None,
    daily_number: int | None = None,
) -> list[uuid.UUID]:
    with tenant_context(tenant_id):
        rows = await list_requests_sent_by_services(
            frozenset(service_keys),
            limit=200,
            room_number=room_number,
            daily_number=daily_number,
        )
    return [row.id for row in rows]


async def test_snapshot_keeps_creator_services(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Форма уборки — `{housekeeping}`, двух служб — обе по алфавиту; менеджер
    (служб нет) и путь без личности (гость, ИИ) — пусто."""
    tenant_a, _ = two_tenants
    maintenance = await make_category(tenant_a, "maintenance", "Техника")

    by_maid = await _create(tenant_a, maintenance, await _actor("housekeeping"))
    by_two = await _create(tenant_a, maintenance, await _actor("laundry", "housekeeping"))
    by_manager = await _create(tenant_a, maintenance, await _actor())
    by_guest = await _create(tenant_a, maintenance, None)

    assert await _snapshot(tenant_a, by_maid.id) == ["housekeeping"]
    assert await _snapshot(tenant_a, by_two.id) == ["housekeeping", "laundry"]
    assert await _snapshot(tenant_a, by_manager.id) == []
    assert await _snapshot(tenant_a, by_guest.id) == []


async def test_sent_shows_foreign_service_requests_of_own_services(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Уборка → техника: видна уборке во «Отправленных» в любом статусе, технике —
    нет (это её очередь); заявки гостя и менеджера ни в чьи «Отправленные» не идут."""
    tenant_a, _ = two_tenants
    await make_category(tenant_a, "housekeeping", "Уборка")
    maintenance = await make_category(tenant_a, "maintenance", "Техника")
    maid = await _actor("housekeeping")

    sent = await _create(tenant_a, maintenance, maid)
    done = await _create(tenant_a, maintenance, maid, summary="лампа")
    with tenant_context(tenant_a):
        await change_request_status(done.id, RequestStatus.IN_PROGRESS)
        await change_request_status(done.id, RequestStatus.DONE)
    await _create(tenant_a, maintenance, None, summary="гость")
    await _create(tenant_a, maintenance, await _actor(), summary="менеджер")

    assert await _sent_ids(tenant_a, "housekeeping") == [done.id, sent.id]
    assert await _sent_ids(tenant_a, "maintenance") == []
    assert await _sent_ids(tenant_a) == []


async def test_transferred_back_leaves_sent(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Переданная обратно в уборку заявка из «Отправленных» уходит — она в очереди уборки."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a, "housekeeping", "Уборка")
    maintenance = await make_category(tenant_a, "maintenance", "Техника")
    request = await _create(tenant_a, maintenance, await _actor("housekeeping"))

    with tenant_context(tenant_a):
        await transfer_request(request.id, housekeeping.id)

    assert await _sent_ids(tenant_a, "housekeeping") == []


async def test_two_services_and_reception(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Сотрудник уборки и прачечной отправил в прачечную: коллега только из уборки
    видит заявку, а у кого есть прачечная — нет (она в его очереди). Ресепшен
    видит отправленное ресепшеном."""
    tenant_a, _ = two_tenants
    laundry = await make_category(tenant_a, "laundry", "Прачечная")
    housekeeping = await make_category(tenant_a, "housekeeping", "Уборка")
    to_laundry = await _create(tenant_a, laundry, await _actor("housekeeping", "laundry"))
    by_reception = await _create(tenant_a, housekeeping, await _actor("reception"))

    assert await _sent_ids(tenant_a, "housekeeping") == [to_laundry.id]
    assert await _sent_ids(tenant_a, "housekeeping", "laundry") == []
    assert await _sent_ids(tenant_a, "reception") == [by_reception.id]


async def test_sent_search_by_room_and_number(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    maintenance = await make_category(tenant_a, "maintenance", "Техника")
    maid = await _actor("housekeeping")
    room_704 = await _create(tenant_a, maintenance, maid, room_number="704")
    room_512 = await _create(tenant_a, maintenance, maid, room_number="512")

    assert await _sent_ids(tenant_a, "housekeeping", room_number="704") == [room_704.id]
    assert await _sent_ids(tenant_a, "housekeeping", daily_number=room_512.daily_number) == [
        room_512.id
    ]


async def test_sent_is_tenant_isolated(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Заявка отеля A с теми же ключами служб в отеле B не видна (RLS, P-4)."""
    tenant_a, tenant_b = two_tenants
    maintenance_a = await make_category(tenant_a, "maintenance", "Техника")
    await make_category(tenant_b, "maintenance", "Техника")
    request = await _create(tenant_a, maintenance_a, await _actor("housekeeping"))

    assert await _sent_ids(tenant_a, "housekeeping") == [request.id]
    assert await _sent_ids(tenant_b, "housekeeping") == []
