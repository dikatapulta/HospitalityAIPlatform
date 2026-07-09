"""Тесты слоя БД (Task 0008): миграция на чистую БД, канон сессии, канон UTC.

Каждый тест получает СВЕЖУЮ временную базу: фикстура создаёт её, прогоняет
`alembic upgrade head` и удаляет после теста — так каждый прогон заодно
проверяет применимость миграций на чистый Postgres. Канонический путь
(`session_scope`) тестируется как есть: фикстура лишь указывает настройкам
окружения на временную базу.

Нужен работающий Postgres (`make dev`); без него тесты пропускаются локально
и падают в CI (там Postgres — обязательный сервис).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import StatementError

from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import UTCDateTime, get_engine, session_scope, utc_now

REPO_ROOT = Path(__file__).resolve().parents[1]


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
# Фикстуры временной БД
# ---------------------------------------------------------------------------


async def _admin_execute(sql: str) -> None:
    settings = get_settings()
    connection = await asyncpg.connect(settings.postgres_dsn, timeout=2)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _postgres_available() -> bool:
    try:
        asyncio.run(_admin_execute("SELECT 1"))
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return False
    return True


@pytest.fixture
def migrated_database_name() -> Iterator[str]:
    """Чистая временная БД с применённой миграцией `head`."""
    if not _postgres_available():
        if os.environ.get("CI"):
            pytest.fail("В CI Postgres обязан быть доступен (сервис в ci.yml)")
        pytest.skip("Postgres недоступен — поднимите локальную среду: make dev")

    database_name = f"hospitality_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(f'CREATE DATABASE "{database_name}"'))
    try:
        settings = get_settings()
        alembic_config = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        alembic_config.set_main_option(
            "sqlalchemy.url",
            f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{database_name}",
        )
        command.upgrade(alembic_config, "head")
        yield database_name
    finally:
        asyncio.run(_admin_execute(f'DROP DATABASE "{database_name}" WITH (FORCE)'))


@pytest.fixture
async def canonical_database(
    migrated_database_name: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Направляет канонический путь (настройки → engine → session_scope) на временную БД."""
    monkeypatch.setenv("POSTGRES_DB", migrated_database_name)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        yield
    finally:
        await get_engine().dispose()
        get_settings.cache_clear()
        get_engine.cache_clear()


# ---------------------------------------------------------------------------
# Миграция и канон сессии — на живом Postgres
# ---------------------------------------------------------------------------


async def test_migration_creates_tenants_table(canonical_database: None) -> None:
    async with session_scope() as session:
        table_exists = await session.scalar(
            text("SELECT to_regclass('public.tenants') IS NOT NULL")
        )
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert table_exists is True
    assert version == "0001"


async def test_session_scope_commits_on_success(canonical_database: None) -> None:
    async with session_scope() as session:
        session.add(Tenant(slug="demo-hotel", name="Demo Hotel"))

    async with session_scope() as session:
        tenant = (await session.execute(select(Tenant))).scalar_one()
    assert tenant.slug == "demo-hotel"
    assert tenant.created_at.utcoffset() == timedelta(0)


async def test_session_scope_rolls_back_on_error(canonical_database: None) -> None:
    with pytest.raises(RuntimeError, match="expected"):
        async with session_scope() as session:
            session.add(Tenant(slug="ghost", name="Ghost Hotel"))
            await session.flush()
            raise RuntimeError("expected")

    async with session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(Tenant))
    assert count == 0


async def test_utc_datetime_roundtrip_preserves_instant(canonical_database: None) -> None:
    almaty = timezone(timedelta(hours=5))
    checkout_local = datetime(2026, 7, 9, 12, 0, tzinfo=almaty)

    async with session_scope() as session:
        session.add(Tenant(slug="roundtrip", name="Roundtrip", created_at=checkout_local))

    async with session_scope() as session:
        tenant = (await session.execute(select(Tenant))).scalar_one()
    assert tenant.created_at == checkout_local  # тот же момент…
    assert tenant.created_at.utcoffset() == timedelta(0)  # …но прочитан как UTC


async def test_naive_datetime_write_fails_loudly(canonical_database: None) -> None:
    with pytest.raises(StatementError, match="naive datetime"):
        async with session_scope() as session:
            session.add(
                Tenant(
                    slug="naive",
                    name="Naive Hotel",
                    created_at=datetime(2026, 7, 9, 12, 0),  # noqa: DTZ001 — суть теста
                )
            )
            await session.flush()
