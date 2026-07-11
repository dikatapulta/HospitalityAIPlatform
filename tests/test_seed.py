"""Тесты сида демо-тенанта и чтения конфига через БД (Task 0011).

DoD задачи: сид идемпотентен (повторный запуск не дублирует и не перезаписывает),
конфиг читается сервисом через канонический `load_tenant_config`.
Нужен работающий Postgres (`make dev`) — как у всех DB-тестов (conftest).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from hospitality.platform.config import (
    TENANT_CONFIG_INVALID_ERROR_CODE,
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TENANT_NOT_FOUND_ERROR_CODE,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.platform.seed import DEMO_TENANT_SLUG, demo_tenant_config, seed_demo_tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError

pytestmark = pytest.mark.usefixtures("canonical_database")


async def _demo_tenant_count() -> int:
    async with platform_session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG)
        )
    return count or 0


async def test_seed_creates_demo_tenant_with_readable_config() -> None:
    """DoD: после сида демо-тенант существует и конфиг читается сервисом."""
    tenant_id = await seed_demo_tenant()

    assert await _demo_tenant_count() == 1
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config == demo_tenant_config()
    assert config.timezone == "Asia/Almaty"


async def test_seed_is_idempotent() -> None:
    """DoD: повторный запуск не дублирует тенанта."""
    first_id = await seed_demo_tenant()
    second_id = await seed_demo_tenant()

    assert first_id == second_id
    assert await _demo_tenant_count() == 1


async def test_seed_does_not_overwrite_existing_config() -> None:
    """Правки конфига (руками/онбордингом) переживают повторный сид на деплое."""
    tenant_id = await seed_demo_tenant()
    customized = demo_tenant_config().model_copy(update={"default_language": "kk"})
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, customized)

    await seed_demo_tenant()

    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config.default_language == "kk"


async def test_seed_fills_config_of_unconfigured_demo_tenant() -> None:
    """Тенант, созданный до Task 0011 без конфига, дозаполняется, а не дублируется."""
    async with platform_session_scope() as session:
        bare_tenant = Tenant(slug=DEMO_TENANT_SLUG, name="Demo Hotel")
        session.add(bare_tenant)
        await session.flush()
        bare_tenant_id = bare_tenant.id

    seeded_id = await seed_demo_tenant()

    assert seeded_id == bare_tenant_id
    assert await _demo_tenant_count() == 1
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, bare_tenant_id)
    assert config == demo_tenant_config()


async def test_load_config_of_unknown_tenant_raises_not_found() -> None:
    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, uuid.uuid4())
    assert error.value.code == TENANT_NOT_FOUND_ERROR_CODE


async def test_load_config_of_unconfigured_tenant_raises() -> None:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="bare-hotel", name="Bare Hotel")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, tenant_id)
    assert error.value.code == TENANT_NOT_CONFIGURED_ERROR_CODE


async def test_load_config_failing_schema_raises_invalid() -> None:
    """Дрейф данных (конфиг в БД не проходит схему) — явная ошибка, не молчание."""
    tenant_id = await seed_demo_tenant()
    async with platform_session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        # Нарочно мимо store_tenant_config: моделируем несовместимый дрейф.
        tenant.config = {"schema_version": 999}

    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, tenant_id)
    assert error.value.code == TENANT_CONFIG_INVALID_ERROR_CODE


async def test_store_config_for_unknown_tenant_raises_not_found() -> None:
    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await store_tenant_config(session, uuid.uuid4(), demo_tenant_config())
    assert error.value.code == TENANT_NOT_FOUND_ERROR_CODE


async def test_stored_config_roundtrips_through_schema() -> None:
    """store → load возвращает эквивалентную модель (JSONB не теряет форму)."""
    tenant_id = await seed_demo_tenant()
    updated = TenantConfig.model_validate(
        {
            "profile": {"city": "Astana", "country_code": "KZ"},
            "timezone": "Asia/Almaty",
            "default_language": "en",
        }
    )
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, updated)

    async with platform_session_scope() as session:
        assert await load_tenant_config(session, tenant_id) == updated
