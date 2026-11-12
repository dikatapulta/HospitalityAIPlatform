"""Кэш конфига тенанта (#168, `platform/config_cache.py`).

Попадание в кэш видно по правке колонки мимо канона: база уже другая, а
`load_tenant_config` отдаёт прежнее, пока строка кэша жива. Канонические
записи (`store_tenant_config`, `mutate_tenant_config`) обязаны быть видны сразу.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from hospitality.platform import config_cache
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TENANT_NOT_FOUND_ERROR_CODE,
    TenantConfig,
    load_tenant_config,
    mutate_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.platform.seed import demo_tenant_config
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError

pytestmark = pytest.mark.usefixtures("canonical_database")

_SET_LANGUAGE_SQL = (
    "UPDATE tenants SET config = jsonb_set(config, '{default_language}', to_jsonb(CAST(:lang AS text))) "
    "WHERE id = :id"
)


async def _tenant(*, configured: bool = True) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=f"cache-{uuid.uuid4().hex[:8]}", name="Cache")
        session.add(tenant)
        await session.flush()
        if configured:
            await store_tenant_config(session, tenant.id, demo_tenant_config())
        return tenant.id


async def _load(tenant_id: uuid.UUID) -> TenantConfig:
    async with platform_session_scope() as session:
        return await load_tenant_config(session, tenant_id)


async def _set_language_bypassing_canon(tenant_id: uuid.UUID, lang: str) -> None:
    async with platform_session_scope() as session:
        await session.execute(text(_SET_LANGUAGE_SQL), {"lang": lang, "id": tenant_id})


def _with_language(lang: str) -> TenantConfig:
    return demo_tenant_config().model_copy(update={"default_language": lang})


async def test_second_read_within_ttl_comes_from_cache() -> None:
    tenant_id = await _tenant()
    assert (await _load(tenant_id)).default_language == "ru"
    await _set_language_bypassing_canon(tenant_id, "kk")
    assert (await _load(tenant_id)).default_language == "ru"


async def test_expired_entry_is_read_from_the_database_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(config_cache, "time", SimpleNamespace(monotonic=lambda: now[0]))
    tenant_id = await _tenant()
    await _load(tenant_id)
    await _set_language_bypassing_canon(tenant_id, "kk")
    now[0] += config_cache.TENANT_CONFIG_CACHE_TTL_SECONDS
    assert (await _load(tenant_id)).default_language == "kk"


async def test_store_is_visible_to_the_next_read() -> None:
    tenant_id = await _tenant()
    await _load(tenant_id)
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, _with_language("kk"))
    assert (await _load(tenant_id)).default_language == "kk"


async def test_mutate_is_visible_to_the_next_read() -> None:
    tenant_id = await _tenant()
    await _load(tenant_id)
    async with platform_session_scope() as session:
        await mutate_tenant_config(
            session, tenant_id, lambda config: config.model_copy(update={"default_language": "kk"})
        )
    assert (await _load(tenant_id)).default_language == "kk"


async def test_read_between_write_and_commit_does_not_outlive_the_commit() -> None:
    """Чужое чтение до commit кладёт в кэш прежний конфиг — commit его сбрасывает."""
    tenant_id = await _tenant()
    async with platform_session_scope() as writer:
        await store_tenant_config(writer, tenant_id, _with_language("kk"))
        await writer.flush()
        assert (await _load(tenant_id)).default_language == "ru"
    assert (await _load(tenant_id)).default_language == "kk"


async def test_writer_reads_its_own_write_and_rollback_leaves_no_trace() -> None:
    """Незакоммиченная запись видна только своей сессии и в кэш не попадает."""
    tenant_id = await _tenant()
    await _load(tenant_id)
    with pytest.raises(RuntimeError):
        async with platform_session_scope() as writer:
            await store_tenant_config(writer, tenant_id, _with_language("kk"))
            assert (await load_tenant_config(writer, tenant_id)).default_language == "kk"
            raise RuntimeError("откат")
    assert (await _load(tenant_id)).default_language == "ru"


async def test_read_started_before_a_write_is_not_cached() -> None:
    tenant_id = uuid.uuid4()
    async with platform_session_scope() as session:
        started = config_cache.read_started()
        config_cache.forget_on_write(session, tenant_id)
    async with platform_session_scope() as session:
        config_cache.remember(session, tenant_id, {"stale": True}, started=started)
        assert config_cache.lookup(session, tenant_id) is None


async def test_returned_config_does_not_share_containers_with_the_cache() -> None:
    tenant_id = await _tenant()
    (await _load(tenant_id)).staff_chats_by_category["engineering"] = "-100"
    assert "engineering" not in (await _load(tenant_id)).staff_chats_by_category


async def test_tenant_without_config_is_cached_until_onboarding_stores_one() -> None:
    tenant_id = await _tenant(configured=False)
    for _ in range(2):
        with pytest.raises(AppError) as error:
            await _load(tenant_id)
        assert error.value.code == TENANT_NOT_CONFIGURED_ERROR_CODE
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, demo_tenant_config())
    assert (await _load(tenant_id)).default_language == "ru"


async def test_missing_tenant_is_not_cached() -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(AppError) as error:
        await _load(tenant_id)
    assert error.value.code == TENANT_NOT_FOUND_ERROR_CODE
    async with platform_session_scope() as session:
        session.add(Tenant(id=tenant_id, slug=f"late-{tenant_id.hex[:8]}", name="Late"))
        await session.flush()
        await store_tenant_config(session, tenant_id, demo_tenant_config())
    assert (await _load(tenant_id)).default_language == "ru"
