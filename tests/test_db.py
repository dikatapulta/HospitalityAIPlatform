"""Тесты слоя БД (Task 0008/0009): миграции на чистую БД, канон сессии, канон UTC.

Фикстуры временной БД — в conftest.py (общие с обязательным тестом изоляции).
Реестр тенантов — НЕтенантная таблица, поэтому здесь используется
`platform_session_scope`; канон тенантных данных (`session_scope` +
`tenant_context` + RLS) проверяет `tests/test_tenant_isolation.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import StatementError

from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import UTCDateTime, platform_session_scope, utc_now

# ---------------------------------------------------------------------------
# Канон времени (§9) — без БД
# ---------------------------------------------------------------------------


def test_utc_now_is_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_utc_datetime_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        UTCDateTime().process_bind_param(datetime(2026, 7, 9, 12, 0), None)  # type: ignore[arg-type]  # noqa: DTZ001


def test_utc_datetime_normalizes_to_utc() -> None:
    almaty = timezone(timedelta(hours=5))
    value = datetime(2026, 7, 9, 17, 0, tzinfo=almaty)
    bound = UTCDateTime().process_bind_param(value, None)  # type: ignore[arg-type]
    assert bound is not None
    assert bound.utcoffset() == timedelta(0)
    assert bound == value  # тот же момент времени, другое представление


# ---------------------------------------------------------------------------
# Миграции и канон сессии — на живом Postgres
# ---------------------------------------------------------------------------


async def test_migration_creates_tables(canonical_database: None) -> None:
    async with platform_session_scope() as session:
        tenants_exists = await session.scalar(
            text("SELECT to_regclass('public.tenants') IS NOT NULL")
        )
        canary_exists = await session.scalar(
            text("SELECT to_regclass('public.tenant_isolation_canary') IS NOT NULL")
        )
        outbox_exists = await session.scalar(
            text("SELECT to_regclass('public.outbox_events') IS NOT NULL")
        )
    assert tenants_exists is True
    assert canary_exists is True
    assert outbox_exists is True

    # Версия миграций — через владельца схемы: рантайм-роли приложения
    # alembic_version недоступна намеренно (см. миграцию 0002).
    connection = await asyncpg.connect(get_settings().postgres_dsn, timeout=2)
    try:
        version = await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()
    assert version == "0003"


async def test_platform_session_scope_commits_on_success(canonical_database: None) -> None:
    async with platform_session_scope() as session:
        session.add(Tenant(slug="demo-hotel", name="Demo Hotel"))

    async with platform_session_scope() as session:
        tenant = (await session.execute(select(Tenant))).scalar_one()
    assert tenant.slug == "demo-hotel"
    assert tenant.created_at.utcoffset() == timedelta(0)


async def test_platform_session_scope_rolls_back_on_error(canonical_database: None) -> None:
    with pytest.raises(RuntimeError, match="expected"):
        async with platform_session_scope() as session:
            session.add(Tenant(slug="ghost", name="Ghost Hotel"))
            await session.flush()
            raise RuntimeError("expected")

    async with platform_session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(Tenant))
    assert count == 0


async def test_utc_datetime_roundtrip_preserves_instant(canonical_database: None) -> None:
    almaty = timezone(timedelta(hours=5))
    checkout_local = datetime(2026, 7, 9, 12, 0, tzinfo=almaty)

    async with platform_session_scope() as session:
        session.add(Tenant(slug="roundtrip", name="Roundtrip", created_at=checkout_local))

    async with platform_session_scope() as session:
        tenant = (await session.execute(select(Tenant))).scalar_one()
    assert tenant.created_at == checkout_local  # тот же момент…
    assert tenant.created_at.utcoffset() == timedelta(0)  # …но прочитан как UTC


async def test_naive_datetime_write_fails_loudly(canonical_database: None) -> None:
    with pytest.raises(StatementError, match="naive datetime"):
        async with platform_session_scope() as session:
            session.add(
                Tenant(
                    slug="naive",
                    name="Naive Hotel",
                    created_at=datetime(2026, 7, 9, 12, 0),  # noqa: DTZ001 — суть теста
                )
            )
            await session.flush()
