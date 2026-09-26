"""Фикстуры тестов композиционного слоя ai (оркестратор, инструменты, evals).

Инфраструктурные фикстуры (временная БД с миграциями, гигиена контекста)
реимпортируются из `tests/conftest.py` — канонический приём (как в gateway и
модуле requests). `demo_tenant` даёт тенанта с настроенными категориями заявок:
без категорий инструмент `create_service_request` не может быть построен.

`service_requests_enabled` включает `ENABLE_SERVICE_REQUESTS` на один тест:
умолчание инсталляции — режим «только консультации» (заявки выключены), и
тест, предметом которого является САМ инструмент создания заявки, объявляет
нужный ему режим явно, а не полагается на умолчание.

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.modules.requests.api import RequestCategoryCreate, create_category
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
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


@pytest.fixture
def service_requests_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Включённый инструмент создания заявки на время одного теста.

    Канон переопределения настройки в тесте — `tests/test_staff_auth.py`:
    `monkeypatch.setenv` + сброс `lru_cache` настроек до и после.
    """
    monkeypatch.setenv("ENABLE_SERVICE_REQUESTS", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
