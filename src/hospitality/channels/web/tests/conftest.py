"""Фикстуры тестов канала web (spec 0027) — канонический приём Task 0012/0016.

`web_hotel` — настроенный тенант (slug `demo-hotel`, конфиг с телефоном
ресепшена) с категорией заявок и заселённой комнатой 101: почти каждому тесту
канала нужен живой Stay с кодом.

F811 отключён на файл: фикстура-параметр обязана называться как
реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

HOTEL_SLUG = "demo-hotel"
RECEPTION_PHONE = "+7 727 000-00-00"
ROOM = "101"


@dataclass(frozen=True)
class WebHotel:
    """Стенд канала: тенант, заселённая комната и её код заселения."""

    tenant_id: uuid.UUID
    stay_id: uuid.UUID
    access_code: str


@pytest.fixture
async def web_hotel(canonical_database: None) -> WebHotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=HOTEL_SLUG, name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=RECEPTION_PHONE,
            ),
        )
    with tenant_context(tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        result = await check_in(
            StayCheckIn(
                room_number=ROOM,
                check_out_at=utc_now() + timedelta(days=1),
                guest_display_name="Wang Li",
            )
        )
    return WebHotel(tenant_id=tenant_id, stay_id=result.stay.id, access_code=result.access_code)
