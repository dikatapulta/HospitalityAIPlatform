"""Фикстуры тестов модуля housekeeping (spec 0046) — канон
`modules/requests/tests/conftest.py`: общие DB-фикстуры реимпортируются из
`tests/conftest.py`.

F811 отключён на файл: фикстура-параметр (`canonical_database` в `two_tenants`)
обязана называться как реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.housekeeping.api import ActingUser
from hospitality.platform.models import Tenant, User
from hospitality.shared.db import platform_session_scope
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


async def make_acting_user(display_name: str) -> ActingUser:
    """Платформенный User: колонки `*_by_user_id` — FK на `users`, случайный uuid
    БД не пропустит."""
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        return ActingUser(user_id=user.id, display_name=display_name)
