"""Фикстуры тестов канала Telegram (Task 0016).

Инфраструктурные фикстуры (временная БД с миграциями, гигиена контекста)
реимпортируются из `tests/conftest.py` — канонический приём (как в gateway,
requests и ai). `demo_tenant` даёт тенанта со slug `demo-hotel` — под него
маппится чат по умолчанию (`TELEGRAM_TENANT_SLUG`); `two_tenants` — для теста
изоляции на таблицах conversations/messages.

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

# Совпадает с дефолтом Settings.telegram_tenant_slug — маппинг чата Phase 0.
DEMO_TENANT_SLUG = "demo-hotel"


@pytest.fixture
async def demo_tenant(canonical_database: None) -> uuid.UUID:
    """Тенант канала (slug `demo-hotel`) — на него маппится входящий чат."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug=DEMO_TENANT_SLUG, name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        return tenant.id


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B» (канон test_tenant_isolation)."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)
