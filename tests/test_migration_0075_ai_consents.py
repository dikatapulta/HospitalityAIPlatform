"""Миграция 0075 — `tenant_memberships.ai_consents` (spec 0082, issue #985).

Канон `test_migration_0072`: БД до `0073`, членство живого отеля, шаг вперёд. Предмет —
умолчание: живое членство получает пустой набор отметок (согласие не предполагается), а
вставка старым образом без колонки — тоже пустой. Затем шаг назад и снова вперёд.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.conftest import database_dsn, temporary_database

_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000007501")
_LIVE = uuid.UUID("00000000-0000-4000-8000-000000007502")
_OLD_IMAGE = uuid.UUID("00000000-0000-4000-8000-000000007503")
_NOW = datetime.now(UTC)


async def _insert_member(dsn: str, user_id: uuid.UUID) -> None:
    """Членство так, как его пишет образ до 0075: без колонки отметок."""
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        await connection.execute(
            "INSERT INTO users (id, display_name, status, is_platform_admin,"
            " created_at, updated_at) VALUES ($1, 'Сотрудник', 'active', false, $2, $2)",
            user_id,
            _NOW,
        )
        await connection.execute(
            "INSERT INTO tenant_memberships (id, user_id, tenant_id, role_key, status,"
            " created_at, updated_at) VALUES ($1, $2, $3, 'staff', 'active', $4, $4)",
            uuid.uuid4(),
            user_id,
            _TENANT_ID,
            _NOW,
        )
    finally:
        await connection.close()


async def _seed(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        await connection.execute(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            " VALUES ($1, 'hotel-0075', 'Hotel 0075', $2, $2)",
            _TENANT_ID,
            _NOW,
        )
    finally:
        await connection.close()
    await _insert_member(dsn, _LIVE)


async def _consents(dsn: str) -> dict[uuid.UUID, list[str]]:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        rows = await connection.fetch("SELECT user_id, ai_consents FROM tenant_memberships")
    finally:
        await connection.close()
    return {row["user_id"]: list(row["ai_consents"]) for row in rows}


@pytest.fixture
def upgraded() -> Iterator[tuple[str, Config]]:
    with temporary_database("0073") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed(dsn))
        command.upgrade(alembic_config, "0075")
        yield dsn, alembic_config


def test_live_and_old_image_members_have_no_marks(upgraded: tuple[str, Config]) -> None:
    dsn, _ = upgraded
    asyncio.run(_insert_member(dsn, _OLD_IMAGE))
    consents = asyncio.run(_consents(dsn))
    assert consents == {_LIVE: [], _OLD_IMAGE: []}


def test_downgrade_and_upgrade_again(upgraded: tuple[str, Config]) -> None:
    dsn, alembic_config = upgraded
    command.downgrade(alembic_config, "0073")
    command.upgrade(alembic_config, "0075")
    assert asyncio.run(_consents(dsn)) == {_LIVE: []}
