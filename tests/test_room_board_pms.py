"""Доска «Номера» у отеля с выгрузкой Opera (spec 0061 §3 п. 4, §8, §9).

`get_pms_status` отдаёт время последнего применённого снимка; «свободен» доска
показывает только при свежем применённом, иначе клетка серая и над доской
полоса. Тест на верхнем уровне, как `test_pms_cabinet.py`: Fake-адаптер живёт
в `integrations/`, тестам кабинета он не виден (контракт 2 import-linter).
F811 отключён на файл: параметр-фикстура называется как реимпортированная.
"""

# ruff: noqa: F811

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.guests import api as guests_api
from hospitality.modules.guests.models import PmsSyncState
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.ports.pms import pms_or_none, set_pms
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (  # noqa: F401  (фикстуры для pytest)
    HOTEL_SLUG,
    PortalHotel,
    _fake_rate_limit,
    client,
    portal_hotel,
    submit_login,
)

ROOMS = f"/staff/{HOTEL_SLUG}/rooms"
ZONE = ZoneInfo("Asia/Almaty")


@pytest.fixture(autouse=True)
def _board_on() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_ROOM_BOARD", "true")
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@pytest.fixture
def pms(client: AsyncClient) -> Iterator[FakePmsAdapter]:
    """Fake-адаптер в порту — после `client`: `create_app` кладёт свой адаптер."""
    previous = pms_or_none()
    adapter = FakePmsAdapter()
    set_pms(adapter)
    try:
        yield adapter
    finally:
        if previous is not None:
            set_pms(previous)


@pytest.fixture
async def opera_hotel(portal_hotel: PortalHotel, client: AsyncClient) -> PortalHotel:
    """Отель с `pms_source: opera_report` (интервал 5 минут), три номера, вошедший менеджер."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            portal_hotel.tenant_id,
            TenantConfig.model_validate(
                {
                    "profile": HotelProfile(city="Almaty", country_code="KZ"),
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "pms_source": "opera_report",
                    "hotel_rooms": [{"number": "1401"}, {"number": "1402"}],
                }
            ),
        )
    await submit_login(client, portal_hotel.login)
    return portal_hotel


def _statuses(html: str) -> dict[str, str]:
    return dict(re.findall(r'class="room" data-room="([^"]+)"[^>]*data-status="(\w+)"', html))


async def _status(tenant_id: uuid.UUID) -> guests_api.PmsStatus:
    with tenant_context(tenant_id):
        return await guests_api.get_pms_status(interval_minutes=5)


async def _applied_ago(tenant_id: uuid.UUID, minutes: int) -> None:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            state = await session.get(PmsSyncState, tenant_id)
            assert state is not None
            state.last_applied_at = utc_now() - timedelta(minutes=minutes)


async def _sync_one_guest(pms: FakePmsAdapter, tenant_id: uuid.UUID) -> None:
    arrival = utc_now().astimezone(ZONE).date()
    pms.check_in_room(
        tenant_id, "1401", "555111", arrival=arrival, departure=arrival + timedelta(days=2)
    )
    pms.publish(tenant_id)
    await guests_api.sync_stays_from_pms()


async def test_pms_status_carries_last_applied_snapshot(
    opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    tenant_id = opera_hotel.tenant_id
    before = await _status(tenant_id)
    assert (before.last_applied_at, before.applied_stale) == (None, False)

    await _sync_one_guest(pms, tenant_id)
    fresh = await _status(tenant_id)
    await _applied_ago(tenant_id, 16)
    old = await _status(tenant_id)

    assert fresh.last_applied_at is not None
    assert not fresh.applied_stale
    assert old.applied_stale
    assert not old.stale  # снимок свежий, отстало только применение


async def test_fresh_applied_snapshot_shows_vacant_rooms(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    await _sync_one_guest(pms, opera_hotel.tenant_id)

    page = (await client.get(ROOMS)).text

    assert _statuses(page) == {"1401": "occupied", "1402": "vacant"}
    assert "Данные из Opera" not in page
    assert "Свободные номера доска покажет" not in page
    assert 'data-filter-status="unknown"' not in page
    assert 'data-filter-status="ready"' in page


async def test_stale_applied_snapshot_greys_out_and_says_since_when(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    await _sync_one_guest(pms, opera_hotel.tenant_id)
    await _applied_ago(opera_hotel.tenant_id, 30)

    page = (await client.get(ROOMS)).text

    assert _statuses(page) == {"1401": "occupied", "1402": "unknown"}
    assert "Данные из Opera не обновлялись с " in page
    assert "notice-warn" in page


async def test_snapshots_arrive_but_none_applied(
    client: AsyncClient, opera_hotel: PortalHotel, pms: FakePmsAdapter
) -> None:
    pms.publish(opera_hotel.tenant_id)

    page = (await client.get(ROOMS)).text

    assert "Данные из Opera приходят, но кабинет их пока не обработал" in page
    assert _statuses(page) == {"1401": "unknown", "1402": "unknown"}
