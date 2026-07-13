"""Фикстуры тестов композиционного слоя ai (оркестратор, инструменты, evals).

Инфраструктурные фикстуры (временная БД с миграциями, гигиена контекста)
реимпортируются из `tests/conftest.py` — канонический приём (как в gateway и
модуле requests). `demo_tenant` даёт тенанта с настроенными категориями заявок:
без категорий инструмент `create_service_request` не может быть построен.

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.requests.api import RequestCategoryCreate, create_category
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)


@pytest.fixture
async def demo_tenant(canonical_database: None) -> uuid.UUID:
    """Тенант с двумя категориями заявок (housekeeping, engineering)."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="demo-hotel", name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    with tenant_context(tenant_id):
        await create_category(RequestCategoryCreate(key="housekeeping", name="Housekeeping"))
        await create_category(RequestCategoryCreate(key="engineering", name="Engineering"))
    return tenant_id
