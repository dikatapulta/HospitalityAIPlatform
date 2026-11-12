"""Заливка ручного ввода кабинета (#971, находки О2-1 и О2-2 аудита 10.11).

60 заявок-мусора не вытесняют срочную и эскалацию из сигнала ресепшена и менеджера
(список пуша — `tests/test_push_flood.py`); поднятая по сроку остаётся у ресепшена;
упёршийся срез виден в логе.
21-я заявка вручную одной учётки за окно — 429 ERR-REQUESTS-011, у другой учётки свой
счёт. «Отметить выезд» сверяет номер со списком номеров отеля (ERR-HOUSEKEEPING-008),
отметка сверх предела — 429 ERR-HOUSEKEEPING-009.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from structlog.testing import capture_logs

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY, RECEPTION_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import entry_limits, signal
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_queue import _make_request
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
CREATE = f"/staff/{HOTEL_SLUG}/api/requests"
REPORT = f"/staff/{HOTEL_SLUG}/api/turnovers"
FLOOD = 60

_LISTENERS = {
    "receptionist": (StaffRole.STAFF, {RECEPTION_SERVICE_KEY}),
    "manager": (StaffRole.MANAGER, set()),
    "maid": (StaffRole.STAFF, {HOUSEKEEPING_SERVICE_KEY}),
}


@pytest.fixture
def shared_limits(portal_hotel: PortalHotel) -> Iterator[FakeRateLimitRedis]:
    """Один счётчик на тест: autouse-фейк conftest даёт новый на каждый вызов, и счёт
    не копился бы. Свой MonkeyPatch и после `portal_hotel` — довод `_fake_rate_limit`."""
    fake = FakeRateLimitRedis()
    patcher = pytest.MonkeyPatch()
    patcher.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    yield fake
    patcher.undo()


@pytest.fixture
def board_enabled(portal_hotel: PortalHotel) -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _hotel_with_staff(tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Службы уборки и ресепшена с сотрудником в каждой: заявки этих служб на третью
    ступень сразу не идут — туда их поднимает только признак или срок."""
    await store_hotel_config(tenant_id, reminder_after_minutes=30)
    users = {}
    for name, services in (("maid", (HOUSEKEEPING_SERVICE_KEY,)), ("reception", ("reception",))):
        users[name] = await create_staff_user(
            unique_login(), tenant_id=tenant_id, role=StaffRole.STAFF, service_keys=services
        )
    return users


async def _escalation(tenant_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    await _make_request(tenant_id, "якорь", category_key="reception", category_name="Ресепшен")
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=categories["reception"].id,
                origin=requests_api.ServiceRequestOrigin.ESCALATION,
                summary="гость просит человека",
                room_number="512",
            )
        )


async def _flood(tenant_id: uuid.UUID, count: int = FLOOD) -> None:
    """Мусор формы кабинета — в службу уборки, туда же, где срочная."""
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        for index in range(count):
            await requests_api.create_request(
                requests_api.ServiceRequestCreate(
                    category_id=categories[HOUSEKEEPING_SERVICE_KEY].id,
                    origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL,
                    summary=f"мусор {index}",
                    room_number="999",
                    guest_language=None,
                )
            )


async def _heard(tenant_id: uuid.UUID) -> dict[str, set[str]]:
    heard: dict[str, set[str]] = {}
    with tenant_context(tenant_id):
        for name, (role, services) in _LISTENERS.items():
            audience = StaffAudience(
                tenant_id=tenant_id, role_key=role, service_keys=frozenset(services)
            )
            heard[name] = {item.id for item in (await signal.build_signal(audience)).unclaimed}
    return heard


async def test_flood_does_not_push_urgent_and_escalation_out_of_signal(
    portal_hotel: PortalHotel,
) -> None:
    tenant_id = portal_hotel.tenant_id
    await _hotel_with_staff(tenant_id)
    urgent = await _make_request(tenant_id, "течёт вода", room_number="305", is_urgent=True)
    escalation = await _escalation(tenant_id)

    with capture_logs() as logs:
        await _flood(tenant_id)
        heard = await _heard(tenant_id)

    loud = {str(urgent.id), str(escalation.id)}
    assert heard["receptionist"] >= loud
    assert heard["manager"] >= loud
    assert str(urgent.id) in heard["maid"]  # служба слышит свою срочную и под мусором
    assert any(log["event"] == "staff_signal_unclaimed_truncated" for log in logs)


async def test_flood_does_not_push_raised_request_out_of_reception_signal(
    portal_hotel: PortalHotel,
) -> None:
    """Заявку не взяли за два срока — ресепшен слышит «Никто не взял» и под свежим мусором."""
    tenant_id = portal_hotel.tenant_id
    await _hotel_with_staff(tenant_id)
    forgotten = await _make_request(tenant_id, "полотенца", room_number="410")
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE service_requests SET created_at = :at WHERE id = :id"),
                {"at": utc_now() - timedelta(minutes=61), "id": forgotten.id},
            )
    await _flood(tenant_id)

    with tenant_context(tenant_id):
        audience = StaffAudience(
            tenant_id=tenant_id, role_key=StaffRole.STAFF, service_keys=frozenset({"reception"})
        )
        unclaimed = (await signal.build_signal(audience)).unclaimed

    [item] = [item for item in unclaimed if item.id == str(forgotten.id)]
    assert item.is_raised is True


async def test_flagged_scan_puts_urgent_and_escalation_first(portal_hotel: PortalHotel) -> None:
    """Срез с признаком — срочные и эскалации первыми, даже при пределе в одну заявку."""
    tenant_id = portal_hotel.tenant_id
    urgent = await _make_request(tenant_id, "искрит розетка", is_urgent=True)
    escalation = await _escalation(tenant_id)
    old = await _make_request(tenant_id, "старая")
    with tenant_context(tenant_id):
        flagged = await requests_api.list_flagged_unclaimed_requests(
            created_before=utc_now(), limit=2, created_at_or_before=utc_now()
        )

    assert [request.id for request in flagged] == [escalation.id, urgent.id]
    assert old.id not in {request.id for request in flagged}


async def test_21st_manual_request_of_one_account_is_429(
    client: AsyncClient, portal_hotel: PortalHotel, shared_limits: FakeRateLimitRedis
) -> None:
    category = await _make_request(portal_hotel.tenant_id, "служба")
    await submit_login(client, portal_hotel.login)
    payload = {"room_number": "305", "category_id": str(category.category_id), "summary": "вода"}

    statuses = [
        (await client.post(CREATE, json=payload, headers=SAME_ORIGIN)).status_code
        for _ in range(entry_limits.MANUAL_REQUESTS_PER_WINDOW)
    ]
    refused = await client.post(CREATE, json=payload, headers=SAME_ORIGIN)

    assert statuses == [200] * entry_limits.MANUAL_REQUESTS_PER_WINDOW
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "ERR-REQUESTS-011"
    with tenant_context(portal_hotel.tenant_id):
        manual = [
            request
            for request in await requests_api.list_unclaimed_requests(
                created_before=utc_now(), limit=100
            )
            if request.origin is requests_api.ServiceRequestOrigin.STAFF_MANUAL
        ]
    assert len(manual) == entry_limits.MANUAL_REQUESTS_PER_WINDOW

    # У другой учётки — свой счёт.
    colleague = unique_login()
    await create_staff_user(colleague, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
    client.cookies.clear()
    await submit_login(client, colleague)
    assert (await client.post(CREATE, json=payload, headers=SAME_ORIGIN)).status_code == 200


async def _rooms_and_housekeeping(portal_hotel: PortalHotel, rooms: list[str]) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            portal_hotel.tenant_id,
            TenantConfig.model_validate(
                {
                    "profile": HotelProfile(city="Almaty", country_code="KZ"),
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "hotel_rooms": [{"number": number} for number in rooms],
                }
            ),
        )
    await _make_request(portal_hotel.tenant_id, "служба уборки есть")
    await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        service_keys=(HOUSEKEEPING_SERVICE_KEY,),
    )


async def test_checkout_of_unknown_room_is_refused_when_room_list_is_loaded(
    client: AsyncClient, portal_hotel: PortalHotel, board_enabled: None
) -> None:
    await _rooms_and_housekeeping(portal_hotel, ["305", "306"])
    await submit_login(client, portal_hotel.login)

    unknown = await client.post(REPORT, json={"room_number": "9999"}, headers=SAME_ORIGIN)
    known = await client.post(REPORT, json={"room_number": "305"}, headers=SAME_ORIGIN)

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "ERR-HOUSEKEEPING-008"
    assert known.status_code == 200


async def test_checkout_without_room_list_takes_any_number(
    client: AsyncClient, portal_hotel: PortalHotel, board_enabled: None
) -> None:
    await _rooms_and_housekeeping(portal_hotel, [])
    await submit_login(client, portal_hotel.login)

    response = await client.post(REPORT, json={"room_number": "9999"}, headers=SAME_ORIGIN)

    assert response.status_code == 200


async def test_checkout_report_over_the_limit_of_one_account_is_429(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    board_enabled: None,
    shared_limits: FakeRateLimitRedis,
) -> None:
    await _rooms_and_housekeeping(portal_hotel, [])
    await submit_login(client, portal_hotel.login)
    limit = entry_limits.TURNOVER_REPORTS_PER_WINDOW

    statuses = {
        (
            await client.post(REPORT, json={"room_number": f"R{index}"}, headers=SAME_ORIGIN)
        ).status_code
        for index in range(limit)
    }
    refused = await client.post(REPORT, json={"room_number": "R-last"}, headers=SAME_ORIGIN)

    assert statuses == {200}
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "ERR-HOUSEKEEPING-009"
