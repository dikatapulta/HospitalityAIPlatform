"""Миграция 0072 — `staff_invites.revoked_at` (spec 0037 §4, issue #944).

Канон `test_migration_0032`: БД до `0070`, строки живого отеля, шаг вперёд. Предмет —
бэкфилл: приглашение, мёртвое к выкату, становится отозванным (отличить отозванное
от истёкшего в нём нечем, а воскресить в списке отозванное нельзя); живое и
принятое не трогаются. Затем шаг назад и снова вперёд.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.conftest import database_dsn, temporary_database

_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000007201")
_MANAGER = uuid.UUID("00000000-0000-4000-8000-000000007202")
_DEAD = uuid.UUID("00000000-0000-4000-8000-000000007203")
_LIVE = uuid.UUID("00000000-0000-4000-8000-000000007204")
_ACCEPTED = uuid.UUID("00000000-0000-4000-8000-000000007205")
_NOW = datetime.now(UTC)


async def _seed(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        await connection.execute(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            " VALUES ($1, 'hotel-0072', 'Hotel 0072', $2, $2)",
            _TENANT_ID,
            _NOW,
        )
        await connection.execute(
            "INSERT INTO users (id, display_name, status, is_platform_admin,"
            " created_at, updated_at) VALUES ($1, 'Менеджер', 'active', false, $2, $2)",
            _MANAGER,
            _NOW,
        )
        for invite_id, login, expires_at, accepted_at in (
            (_DEAD, "DEAD", _NOW - timedelta(hours=1), None),
            (_LIVE, "LIVE", _NOW + timedelta(hours=1), None),
            (_ACCEPTED, "DONE", _NOW - timedelta(hours=1), _NOW - timedelta(hours=2)),
        ):
            await connection.execute(
                "INSERT INTO staff_invites (id, tenant_id, role_key, invited_name, login,"
                " token_hash, invited_by, expires_at, accepted_at, created_at)"
                " VALUES ($1, $2, 'staff', 'Сотрудник', $3, $4, $5, $6, $7, $8)",
                invite_id,
                _TENANT_ID,
                login,
                uuid.uuid4().hex,
                _MANAGER,
                expires_at,
                accepted_at,
                _NOW - timedelta(days=1),
            )
    finally:
        await connection.close()


async def _revoked(dsn: str) -> dict[uuid.UUID, datetime | None]:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        rows = await connection.fetch("SELECT id, expires_at, revoked_at FROM staff_invites")
    finally:
        await connection.close()
    return {row["id"]: row["revoked_at"] for row in rows}


@pytest.fixture
def upgraded() -> Iterator[tuple[str, Config]]:
    with temporary_database("0070") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed(dsn))
        command.upgrade(alembic_config, "0072")
        yield dsn, alembic_config


def test_dead_invites_become_revoked(upgraded: tuple[str, Config]) -> None:
    dsn, _ = upgraded
    revoked = asyncio.run(_revoked(dsn))
    assert revoked[_DEAD] is not None
    assert revoked[_LIVE] is None
    assert revoked[_ACCEPTED] is None


def test_downgrade_and_upgrade_again(upgraded: tuple[str, Config]) -> None:
    dsn, alembic_config = upgraded
    command.downgrade(alembic_config, "0070")
    command.upgrade(alembic_config, "0072")
    assert asyncio.run(_revoked(dsn))[_LIVE] is None
