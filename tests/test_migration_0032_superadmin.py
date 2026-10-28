"""Миграция 0032 — суперадмин (spec 0044 §7, issue #377).

Канон `test_migration_0028`: БД поднимается до `0032`, наполняется строками,
какие оставил бы живой отель (стёртая учётка, суперадмин с паролем и вторым
фактором, ссылка нового пароля), и делается шаг назад. Предмет — что увидит
старый образ после `downgrade`:

- стёртая учётка — `deactivated`: статуса `erased` старый код не читает
  (`LookupError` в SQLAlchemy `Enum`), а войти ею и так нельзя;
- у суперадмина стёрт хэш пароля: второго фактора старый код не знает и пустил
  бы его по одному паролю;
- новые таблицы удалены, и повторный `upgrade` проходит.
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

_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000003201")
_ERASED_USER = uuid.UUID("00000000-0000-4000-8000-000000003202")
_ADMIN_USER = uuid.UUID("00000000-0000-4000-8000-000000003203")
_STAFF_USER = uuid.UUID("00000000-0000-4000-8000-000000003204")
_STAFF_IDENTITY = uuid.UUID("00000000-0000-4000-8000-000000003205")
_CREATED_AT = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


async def _seed(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        await connection.execute(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            " VALUES ($1, 'hotel-0032', 'Hotel 0032', $2, $2)",
            _TENANT_ID,
            _CREATED_AT,
        )
        for user_id, status, is_admin, login in (
            (_ERASED_USER, "erased", False, "OLDY"),
            (_ADMIN_USER, "active", True, "ADMN"),
            (_STAFF_USER, "active", False, "SESA"),
        ):
            await connection.execute(
                "INSERT INTO users (id, display_name, status, is_platform_admin,"
                " created_at, updated_at) VALUES ($1, 'Сотрудник', $2, $3, $4, $4)",
                user_id,
                status,
                is_admin,
                _CREATED_AT,
            )
            await connection.execute(
                "INSERT INTO user_identities (id, user_id, kind, external_id, secret_hash,"
                " created_at) VALUES ($1, $2, 'password', $3, 'hash', $4)",
                _STAFF_IDENTITY if user_id == _STAFF_USER else uuid.uuid4(),
                user_id,
                f"{_TENANT_ID}:{login}",
                _CREATED_AT,
            )
        await connection.execute(
            "INSERT INTO staff_second_factors (user_id, totp_secret, created_at, updated_at)"
            " VALUES ($1, 'SECRET', $2, $2)",
            _ADMIN_USER,
            _CREATED_AT,
        )
        await connection.execute(
            "INSERT INTO staff_password_resets (id, identity_id, token_hash, issued_by,"
            " expires_at, created_at) VALUES ($1, $2, $3, $4, $5, $5)",
            uuid.uuid4(),
            _STAFF_IDENTITY,
            uuid.uuid4().hex,
            _ADMIN_USER,
            _CREATED_AT,
        )
    finally:
        await connection.close()


async def _fetch(dsn: str, query: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        return list(await connection.fetch(query))
    finally:
        await connection.close()


@pytest.fixture
def downgraded() -> Iterator[tuple[str, Config]]:
    with temporary_database("0032") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed(dsn))
        command.downgrade(alembic_config, "0031")
        yield dsn, alembic_config


def test_downgrade_leaves_data_the_old_image_can_read(downgraded: tuple[str, Config]) -> None:
    dsn, _ = downgraded
    statuses = {
        row["id"]: row["status"] for row in asyncio.run(_fetch(dsn, "SELECT id, status FROM users"))
    }
    assert statuses == {
        _ERASED_USER: "deactivated",
        _ADMIN_USER: "active",
        _STAFF_USER: "active",
    }
    hashes = {
        row["user_id"]: row["secret_hash"]
        for row in asyncio.run(_fetch(dsn, "SELECT user_id, secret_hash FROM user_identities"))
    }
    assert hashes[_ADMIN_USER] is None
    assert hashes[_STAFF_USER] == "hash"
    tables = {
        row["table_name"]
        for row in asyncio.run(_fetch(dsn, "SELECT table_name FROM information_schema.tables"))
    }
    assert not {"staff_second_factors", "staff_password_resets"} & tables


def test_upgrade_again_after_downgrade(downgraded: tuple[str, Config]) -> None:
    dsn, alembic_config = downgraded
    command.upgrade(alembic_config, "0032")
    rows = asyncio.run(_fetch(dsn, "SELECT count(*) AS n FROM staff_second_factors"))
    assert rows[0]["n"] == 0
