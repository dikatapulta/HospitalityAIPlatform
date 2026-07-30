"""Фикстуры тестов общего ядра каналов (spec 0027 §2, spec 0032).

Инфраструктурные фикстуры (временная БД с миграциями, гигиена контекста)
реимпортируются из `tests/conftest.py` — канонический приём (как в телеграм-
канале, gateway и requests). `demo_tenant`/`two_tenants` — копия фикстур
`channels/telegram/tests/conftest.py`: у общего ядра те же таблицы
conversations/messages, но свои сценарии (ретеншн, issue #42).

F811 отключён на файл: фикстура-параметр обязана называться как
реимпортированная фикстура — так pytest связывает их.
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


@pytest.fixture
async def demo_tenant(canonical_database: None) -> uuid.UUID:
    """Один тенант в реестре — базовый сценарий ретеншна."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="demo-hotel", name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        return tenant.id


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B» (канон test_tenant_isolation).

    Оба БЕЗ конфига: ретеншн обязан обходить и тенантов с незавершённым
    онбордингом (spec 0032 §3, `list_tenant_ids`).
    """
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)
