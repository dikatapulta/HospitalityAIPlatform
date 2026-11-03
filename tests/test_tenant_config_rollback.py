"""Откат образа через новое поле конфига тенанта (#387).

Новый образ пишет в `tenants.config` поле, которого старый код не знает. После
отката старый код обязан конфиг прочитать — иначе бот отеля остаётся без
справочника, а страница чата гостя отвечает 500, — и обязан не стереть это поле
своей записью, иначе настройка пропадёт при возврате на новый образ.
Другой дрейф схемы по-прежнему ERR-PLATFORM-006.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text

from hospitality.platform.config import (
    TENANT_CONFIG_INVALID_ERROR_CODE,
    HotelFact,
    TenantConfig,
    load_tenant_config,
    mutate_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.platform.seed import demo_tenant_config
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from tests.test_logging import find_record, read_log_records

pytestmark = pytest.mark.usefixtures("canonical_database")

# Так поле кладёт образ новее этого кода: верхний уровень и внутри раздела.
NEWER_FIELDS_SQL = (
    "UPDATE tenants SET config = jsonb_set("
    'config || \'{"hotel_rooms": ["1301", "1302"]}\', '
    "'{profile,district}', '\"Медеу\"') WHERE id = :id"
)


async def _tenant_with_newer_fields(slug: str) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        await store_tenant_config(session, tenant.id, demo_tenant_config())
        await session.flush()
        await session.execute(text(NEWER_FIELDS_SQL), {"id": tenant.id})
        return tenant.id


async def _stored_config(tenant_id: uuid.UUID) -> dict[str, Any]:
    async with platform_session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None and tenant.config is not None
    return tenant.config


def _with_fact(topic: str) -> Callable[[TenantConfig], TenantConfig]:
    def mutate(config: TenantConfig) -> TenantConfig:
        fact = HotelFact(topic=topic, answer="Есть")
        return config.model_copy(update={"hotel_facts": [*config.hotel_facts, fact]})

    return mutate


async def test_config_with_fields_of_a_newer_image_is_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id = await _tenant_with_newer_fields("rollback-read-hotel")
    configure_logging()

    async with platform_session_scope() as session:
        loaded = await load_tenant_config(session, tenant_id)

    assert loaded == demo_tenant_config()
    record = find_record(read_log_records(capsys), "tenant_config_unknown_fields_ignored")
    assert record["level"] == "warning"
    assert record["tenant_id"] == str(tenant_id)
    assert sorted(record["fields"]) == ["hotel_rooms", "profile.district"]


async def test_other_schema_drift_is_still_config_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Пропуск незнакомых полей не глотает настоящий дрейф: значение вне схемы.

    Причину оператор видит в журнале (#23): путь поля и тип нарушения, без
    значения — незнакомое поле в ней не числится, его пропуск — не дрейф.
    """
    tenant_id = await _tenant_with_newer_fields("rollback-drift-hotel")
    async with platform_session_scope() as session:
        await session.execute(
            text(
                'UPDATE tenants SET config = config || \'{"default_language": "russian"}\' '
                "WHERE id = :id"
            ),
            {"id": tenant_id},
        )
    configure_logging()

    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, tenant_id)
    assert error.value.code == TENANT_CONFIG_INVALID_ERROR_CODE
    records = read_log_records(capsys)
    record = find_record(records, "tenant_config_invalid")
    assert record["level"] == "warning"
    assert record["tenant_id"] == str(tenant_id)
    assert record["errors"] == [{"field": "default_language", "type": "string_pattern_mismatch"}]
    assert "russian" not in str(records)


async def test_store_keeps_top_level_fields_of_a_newer_image() -> None:
    tenant_id = await _tenant_with_newer_fields("rollback-store-hotel")
    rewritten = demo_tenant_config().model_copy(update={"default_language": "kk"})

    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, rewritten)

    stored = await _stored_config(tenant_id)
    assert stored["hotel_rooms"] == ["1301", "1302"]
    assert stored["default_language"] == "kk"
    # Раздел пишется целиком из схемы этого кода: вложенное поле не сохраняется.
    assert "district" not in stored["profile"]


async def test_mutate_keeps_top_level_fields_of_a_newer_image() -> None:
    tenant_id = await _tenant_with_newer_fields("rollback-mutate-hotel")

    async with platform_session_scope() as session:
        updated = await mutate_tenant_config(session, tenant_id, _with_fact("Парковка"))

    stored = await _stored_config(tenant_id)
    assert stored["hotel_rooms"] == ["1301", "1302"]
    assert [fact["topic"] for fact in stored["hotel_facts"]] == ["Парковка"]
    assert [fact.topic for fact in updated.hotel_facts] == ["Парковка"]
