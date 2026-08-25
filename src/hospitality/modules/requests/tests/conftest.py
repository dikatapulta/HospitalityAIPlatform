"""Фикстуры тестов модуля requests (Task 0012).

Тесты модуля живут внутри модуля (анатомия §5.2), но инфраструктурные
фикстуры (временная БД с миграциями, гигиена контекста логов и реестра
подписчиков) — общие для всего репозитория и живут в `tests/conftest.py`.
Реимпорт ниже делает их видимыми pytest'у и в этом дереве — канонический
приём для тестов каждого нового модуля (корень репозитория добавлен в
`pythonpath` в pyproject.toml).

F811 отключён на файл: фикстура-параметр (`canonical_database` в `two_tenants`)
обязана называться как реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import (
    ActingUser,
    RequestCategoryCreate,
    RequestCategoryRead,
    create_category,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.platform.models import Tenant, User
from hospitality.shared.db import platform_session_scope, session_scope
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


async def make_category(
    tenant_id: uuid.UUID, key: str = "housekeeping", name: str = "Housekeeping"
) -> RequestCategoryRead:
    """Категория от имени тенанта — общий шаг почти каждого теста модуля."""
    with tenant_context(tenant_id):
        return await create_category(RequestCategoryCreate(key=key, name=name))


async def make_acting_user(display_name: str) -> ActingUser:
    """Платформенный User для acting_user: колонки `claimed_by_user_id` и
    `closed_by_user_id` — FK на `users` (миграции 0018 и 0025), случайный uuid
    БД не пропустит."""
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        return ActingUser(user_id=user.id, display_name=display_name)


async def read_closed_by(request_id: uuid.UUID) -> tuple[uuid.UUID | None, str | None]:
    """Пара `closed_by_*` прямо из строки БД.

    Из пары «кто закрыл» наружу модуля отдаётся только момент (spec 0035 §13:
    имени закрывшего в `ServiceRequestRead` нет), поэтому тесты читают его из
    ORM — вызывается уже внутри `tenant_context`, RLS показывает свою строку.
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(ServiceRequest).where(ServiceRequest.id == request_id))
        ).scalar_one()
        return (row.closed_by_user_id, row.closed_by_display_name)
