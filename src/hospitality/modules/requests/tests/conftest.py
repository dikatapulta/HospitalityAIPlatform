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
from datetime import date, datetime

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import (
    ActingUser,
    RequestCategoryCreate,
    RequestCategoryRead,
    create_category,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.platform.config import (
    DEFAULT_REQUEST_REMINDER_MINUTES,
    HotelProfile,
    TenantConfig,
    store_tenant_config,
)
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


async def store_hotel_config(
    tenant_id: uuid.UUID,
    *,
    timezone: str = "Asia/Almaty",
    reminder_after_minutes: int | None = DEFAULT_REQUEST_REMINDER_MINUTES,
    reminder_minutes_by_category: dict[str, int] | None = None,
) -> None:
    """Конфиг отеля для тестов сводки дня: `two_tenants` его не пишет намеренно
    (модуль обязан работать и до онбординга), поэтому тестам, которым нужны
    часовой пояс или сроки напоминаний, ставят его сами. Копия одноимённого
    помощника `staff_portal/tests/conftest.py` — фикстуры тестов не общие.
    """
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone=timezone,
                default_language="ru",
                request_reminder_after_minutes=reminder_after_minutes,
                request_reminder_minutes_by_category=reminder_minutes_by_category or {},
            ),
        )


async def shift_request(
    request_id: uuid.UUID,
    *,
    service_day: date | None = None,
    created_at: datetime | None = None,
    claimed_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> None:
    """Передвинуть метки времени и день заявки прямо в БД (канон test_retention).

    Ждать реальные сутки нельзя, а подменять «сейчас» значило бы проверять не
    тот код, который поедет. Вызывается внутри `tenant_context` — RLS
    ограничивает UPDATE своей строкой.
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(ServiceRequest).where(ServiceRequest.id == request_id))
        ).scalar_one()
        if service_day is not None:
            row.service_day = service_day
        if created_at is not None:
            row.created_at = created_at
        if claimed_at is not None:
            row.claimed_at = claimed_at
        if closed_at is not None:
            row.closed_at = closed_at
