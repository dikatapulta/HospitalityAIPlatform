"""Фикстуры тестов модуля showroom (spec 0069 §7) — общие DB-фикстуры реимпортируются
из `tests/conftest.py`; отель — тенант с конфигом и витриной, сцена — вымышленное
здание демо-отеля (`tools/demo_showroom.py`).

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.showroom.tests.request_fixtures import scene  # noqa: F401  (запросы)
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _frozen_ratelimit_clock,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

SHOWROOM = ShowroomConfig(enabled=True, sales_category_key="sales")


async def create_hotel(slug: str, *, showroom: ShowroomConfig | None = SHOWROOM) -> uuid.UUID:
    """Тенант с конфигом; `showroom` — настройки витрины (None — витрины нет)."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=f"Hotel {slug}")
        session.add(tenant)
        await session.flush()
        await store_tenant_config(
            session,
            tenant.id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                showroom=showroom,
            ),
        )
        return tenant.id


@pytest.fixture
async def hotel(canonical_database: None) -> uuid.UUID:
    return await create_hotel("showroom-hotel")
