"""Фикстуры тестов модуля guests (spec 0027) — канонический приём Task 0012:
тесты живут внутри модуля (§5.2), инфраструктурные фикстуры реимпортируются
из `tests/conftest.py`.

F811 отключён на файл: фикстура-параметр (`canonical_database` в `two_tenants`)
обязана называться как реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from hospitality.modules.guests.api import StayCheckIn, StayCheckInResult, check_in
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B» (канон test_tenant_isolation)."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def check_in_room(
    tenant_id: uuid.UUID, room_number: str = "101", *, nights: int = 1
) -> StayCheckInResult:
    """Заселение от имени тенанта — общий шаг почти каждого теста модуля."""
    with tenant_context(tenant_id):
        return await check_in(
            StayCheckIn(
                room_number=room_number,
                check_out_at=utc_now() + timedelta(days=nights),
                guest_display_name="Test Guest",
            )
        )
