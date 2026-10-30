"""Фикстуры тестов модуля room_service (spec 0038) — канон
`modules/requests/tests/conftest.py`: общие DB-фикстуры реимпортируются из
`tests/conftest.py`.

F811 отключён на файл: фикстура-параметр (`canonical_database` в `two_tenants`)
обязана называться как реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from hospitality.platform.config import (
    HotelProfile,
    RoomServiceCatalogConfig,
    RoomServiceCatalogKey,
    RoomServiceConfig,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    fake_file_storage,
    migrated_database_name,
)


def room_service_config(*, enabled: bool = True, shop: bool = True) -> RoomServiceConfig:
    """Room service с рестораном (`fnb`, 10%) и, если `shop`, магазином (`supply`, 0%)."""
    catalogs: dict[RoomServiceCatalogKey, RoomServiceCatalogConfig] = {
        "restaurant": RoomServiceCatalogConfig(
            category_key="fnb", service_charge_percent=Decimal("10")
        )
    }
    if shop:
        catalogs["shop"] = RoomServiceCatalogConfig(
            category_key="supply", service_charge_percent=Decimal("0")
        )
    return RoomServiceConfig(enabled=enabled, catalogs=catalogs)


async def configure_tenant(tenant_id: uuid.UUID, room_service: RoomServiceConfig | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                room_service=room_service,
            ),
        )


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта — «Hotel A» и «Hotel B» (канон test_tenant_isolation), у обоих
    room service открыт: изоляцию проверяет RLS, а не закрытый каталог."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        tenants = (tenant_a.id, tenant_b.id)
    for tenant_id in tenants:
        await configure_tenant(tenant_id, room_service_config())
    return tenants
