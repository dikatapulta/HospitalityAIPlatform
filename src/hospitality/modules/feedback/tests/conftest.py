"""Фикстуры тестов модуля feedback (spec 0076 §10) — канон
`modules/table_booking/tests/conftest.py`: общие DB-фикстуры реимпортируются из
`tests/conftest.py`.

Отель: служба эскалаций `reception`, служба заявок `housekeeping`, один Stay.
Заявка `done` закрыта сотрудником с именем — карточка пишет «Закрыл: …».

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant, User
from hospitality.platform.staff_actions import StaffActor
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

STAFF_NAME = "Айгерим"


@dataclass(frozen=True)
class Hotel:
    tenant_id: uuid.UUID
    stay_id: uuid.UUID
    reception_id: uuid.UUID
    housekeeping_id: uuid.UUID
    staff: StaffActor


async def configure(
    tenant_id: uuid.UUID, *, enabled: bool = True, escalation_key: str | None = "reception"
) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                escalation_category_key=escalation_key,
                guest_feedback_enabled=enabled,
            ),
        )


async def make_hotel(slug: str) -> Hotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        user = User(display_name=STAFF_NAME)
        session.add(user)
        await session.flush()
        tenant_id, user_id = tenant.id, user.id
    await configure(tenant_id)
    with tenant_context(tenant_id):
        reception = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        housekeeping = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Хаускипинг")
        )
        stay = await check_in(
            StayCheckIn(room_number="512", check_out_at=utc_now() + timedelta(days=3))
        )
    return Hotel(
        tenant_id=tenant_id,
        stay_id=stay.stay.id,
        reception_id=reception.id,
        housekeeping_id=housekeeping.id,
        staff=StaffActor(user_id=user_id, display_name=STAFF_NAME),
    )


@pytest.fixture
async def hotel(canonical_database: None) -> Hotel:
    return await make_hotel("feedback-hotel")


async def guest_request(
    hotel: Hotel,
    *,
    status: requests_api.RequestStatus = requests_api.RequestStatus.DONE,
    stay_id: uuid.UUID | None = None,
    summary: str = "уборка номера",
) -> requests_api.ServiceRequestRead:
    """Заявка гостя в хаускипинге; по умолчанию — выполненная сотрудником."""
    with tenant_context(hotel.tenant_id):
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=hotel.housekeeping_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary=summary,
                room_number="512",
            ),
            stay_id=stay_id or hotel.stay_id,
        )
        if status is requests_api.RequestStatus.NEW:
            return request
        request = await requests_api.change_request_status(
            request.id, requests_api.RequestStatus.IN_PROGRESS, acting_user=hotel.staff
        )
        if status is requests_api.RequestStatus.IN_PROGRESS:
            return request
        return await requests_api.change_request_status(request.id, status, acting_user=hotel.staff)


async def feedback_cards(hotel: Hotel) -> list[requests_api.ServiceRequestRead]:
    """Карточки `origin=feedback` отеля, старые первыми."""
    with tenant_context(hotel.tenant_id):
        page = await requests_api.list_requests(limit=100, offset=0)
    cards = [r for r in page.items if r.origin is requests_api.ServiceRequestOrigin.FEEDBACK]
    return sorted(cards, key=lambda card: card.created_at)


async def expect_code(code: str, coro: Any) -> None:
    with pytest.raises(AppError) as error:
        await coro
    assert error.value.code == code
