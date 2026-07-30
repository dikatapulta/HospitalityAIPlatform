"""Фикстуры тестов кабинета персонала (spec 0033 §10) — приём канала web.

`portal_hotel` — тенант + менеджер с паролем: почти каждому тесту страниц
нужен сотрудник, которому есть куда войти. Клиент — httpx поверх настоящего
`create_app` с base_url **https** (cookie кабинета Secure — по http httpx её
не вернёт). Rate-limit подменяется фейком на каждый тест: смоук ходит с
одного client_ip (ASGITransport), живой Redis локальной среды копил бы
счётчик успешных входов между прогонами.

F811 отключён на файл: фикстура-параметр обязана называться как
реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.platform.models import StaffRole, Tenant
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    FakeRateLimitRedis,
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)
from tests.test_staff_auth import PASSWORD, create_staff_user

HOTEL_SLUG = "demo-hotel"
HOTEL_NAME = "Demo Hotel"


@dataclass(frozen=True)
class PortalHotel:
    """Стенд кабинета: тенант и менеджер, который может в него войти."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    email: str


@pytest.fixture(autouse=True)
def _fake_rate_limit() -> Iterator[None]:
    """Свой MonkeyPatch, НЕ общая фикстура monkeypatch: autouse-фикстура
    захватила бы её раньше `canonical_database`, и undo POSTGRES_DB случился бы
    ПОСЛЕ дропа временной БД — `_admin_execute` подключился бы к самой
    дропаемой базе (ObjectInUseError)."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        "hospitality.shared.ratelimit.create_redis_client", lambda: FakeRateLimitRedis()
    )
    yield
    patcher.undo()


@pytest.fixture
async def portal_hotel(canonical_database: None) -> PortalHotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=HOTEL_SLUG, name=HOTEL_NAME)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    email = f"manager-{uuid.uuid4().hex[:8]}@hotel.kz"
    user_id = await create_staff_user(
        email, tenant_id=tenant_id, role=StaffRole.MANAGER, display_name="Аружан Менеджер"
    )
    return PortalHotel(tenant_id=tenant_id, user_id=user_id, email=email)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        yield client


async def submit_login(client: AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    """POST формы входа; cookie сессии остаётся в jar клиента."""
    return await client.post("/staff/login", data={"email": email, "password": password})
