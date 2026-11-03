"""Фикстуры тестов модуля table_booking (spec 0064 §7) — канон
`modules/room_service/tests/conftest.py`: общие DB-фикстуры реимпортируются из
`tests/conftest.py`.

Часы модуля замораживаются сдвигом `utc_now` в его файлах, а не подменой пояса
(«сейчас» отеля — 11:00 по Алматы сегодняшнего дня): слоты и запас 30 мин зависят
от момента прогона.

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import answers, bookings, tracking
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.models import Tenant, User
from hospitality.platform.staff_actions import StaffActor
from hospitality.platform.table_booking_config import RestaurantConfig, TableBookingConfig
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

ZONE = ZoneInfo("Asia/Almaty")
TERRACE = RestaurantConfig(
    key="terrace",
    names=LocalizedText(en="Terrace", ru="Терраса"),
    category_key="fnb",
    first_seating=time(12, 0),
    last_seating=time(22, 0),
    max_party=8,
)


def table_booking_config(*, enabled: bool = True) -> TableBookingConfig:
    return TableBookingConfig(enabled=enabled, restaurants=(TERRACE,))


async def configure_tenant(tenant_id: uuid.UUID, booking: TableBookingConfig | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                table_booking=booking,
            ),
        )


@dataclass
class Clock:
    """«Сейчас» модуля: двигается тестом (`clock.now = …`)."""

    now: datetime

    def local(self, day_offset: int, at: time) -> datetime:
        today = self.now.astimezone(ZONE).date()
        return datetime.combine(today + timedelta(days=day_offset), at, tzinfo=ZONE)

    def today(self) -> date:
        return self.now.astimezone(ZONE).date()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    today = utc_now().astimezone(ZONE).date()
    frozen = Clock(datetime.combine(today, time(11, 0), tzinfo=ZONE))
    now: Callable[[], datetime] = lambda: frozen.now  # noqa: E731
    for module in (answers, bookings, tracking):
        monkeypatch.setattr(module, "utc_now", now)
    return frozen


@dataclass(frozen=True)
class Hotel:
    tenant_id: uuid.UUID
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    fnb_id: uuid.UUID
    other_id: uuid.UUID
    staff: StaffActor


async def make_hotel(slug: str, *, nights: int = 3) -> Hotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    await configure_tenant(tenant_id, table_booking_config())
    with tenant_context(tenant_id):
        fnb = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="fnb", name="F&B")
        )
        other = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        stay = await check_in(
            StayCheckIn(
                room_number="101",
                check_out_at=utc_now() + timedelta(days=nights),
                guest_display_name="Wang Li",
            )
        )
    return Hotel(
        tenant_id=tenant_id,
        stay_id=stay.stay.id,
        guest_identity_id=uuid.uuid4(),
        fnb_id=fnb.id,
        other_id=other.id,
        staff=StaffActor(user_id=await _staff_user(), display_name="Айгуль"),
    )


async def _staff_user() -> uuid.UUID:
    """Сотрудник для `answered_by_user_id` (FK на `users`)."""
    async with platform_session_scope() as session:
        user = User(display_name="Айгуль")
        session.add(user)
        await session.flush()
        return user.id


@pytest.fixture
async def hotel(canonical_database: None, clock: Clock) -> Hotel:
    return await make_hotel("demo-hotel")


def create(
    clock: Clock, *, day: int = 0, at: time = time(19, 0), key: str = "key-0001", **extra: Any
) -> table_booking_api.BookingCreate:
    return table_booking_api.BookingCreate(
        client_key=key,
        restaurant_key=extra.pop("restaurant_key", "terrace"),
        date=clock.today() + timedelta(days=day),
        time=at,
        party_size=extra.pop("party_size", 4),
        guest_language=extra.pop("guest_language", "ru"),
        **extra,
    )


async def place(
    hotel: Hotel, data: table_booking_api.BookingCreate
) -> table_booking_api.BookingRead:
    with tenant_context(hotel.tenant_id):
        placed = await table_booking_api.request_booking(
            hotel.stay_id, hotel.guest_identity_id, data
        )
    return placed.booking


async def expect_code(code: str, coro: Any) -> None:
    with pytest.raises(AppError) as error:
        await coro
    assert error.value.code == code
