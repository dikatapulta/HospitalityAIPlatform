"""Фикстуры тестов страницы витрины (spec 0069 §7 п. 3, 7).

`showroom_hotel` — отель с витриной по коду: включена, сцена — вымышленное здание
демо-отеля, код задан командой модуля. Лимиты считает `FakeRateLimitRedis` — один
фейк на тест: счётчик копится между запросами, живой Redis не нужен (канон 0023).

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom.tests.conftest import SHOWROOM, create_hotel
from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showroom import build_demo_scene
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    FakeRateLimitRedis,
    _clean_log_context,
    _frozen_ratelimit_clock,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

SLUG = "showroom-hotel"
CODE = "show-2026-code"


@dataclass(frozen=True)
class ShowroomHotel:
    tenant_id: uuid.UUID


async def set_showroom(tenant_id: uuid.UUID, **changes: object) -> None:
    """Правка настроек витрины отеля (флаги режимов) — каноном конфига."""

    def mutate(config: TenantConfig) -> TenantConfig:
        assert config.showroom is not None
        return config.model_copy(update={"showroom": config.showroom.model_copy(update=changes)})

    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, mutate)


async def create_showroom_hotel(
    slug: str = SLUG, *, showroom: ShowroomConfig = SHOWROOM, code: str | None = CODE
) -> uuid.UUID:
    tenant_id = await create_hotel(slug, showroom=showroom)
    with tenant_context(tenant_id):
        await showroom_api.replace_scene(build_demo_scene(), acting_user=None)
        if code is not None:
            await showroom_api.set_access_code(code)
    return tenant_id


@pytest.fixture
async def showroom_hotel(canonical_database: None) -> ShowroomHotel:
    return ShowroomHotel(tenant_id=await create_showroom_hotel())


@pytest.fixture
def limits() -> Iterator[FakeRateLimitRedis]:
    """Своя MonkeyPatch, не фикстура `monkeypatch`: поднятая раньше `canonical_database`,
    она откатывала бы POSTGRES_DB уже после удаления временной БД (канон conftest кабинета)."""
    fake = FakeRateLimitRedis()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
        yield fake


@pytest.fixture
async def client(limits: FakeRateLimitRedis) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://test"
    ) as client:
        yield client
