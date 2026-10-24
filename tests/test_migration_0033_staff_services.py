"""Миграция 0033 — службы на членстве, уровень вместо роли ресепшена (spec 0045 §7).

Канон `test_migration_0032`: БД поднимается до `0032`, наполняется строками
живого отеля (ресепшен, сотрудник, менеджер, ожидающее приглашение ресепшена),
затем шаг вверх и шаг назад. Предмет:

- вверх: `receptionist` → `staff` + служба `reception` у членств и приглашений,
  прочие получают пустой список служб;
- назад: всё, что старый образ не прочтёт или прочтёт неверно, приведено к его
  виду — служба ресепшена снова роль `receptionist`, `senior` — `staff`.
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

_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000003301")
_RECEPTION_USER = uuid.UUID("00000000-0000-4000-8000-000000003302")
_STAFF_USER = uuid.UUID("00000000-0000-4000-8000-000000003303")
_MANAGER_USER = uuid.UUID("00000000-0000-4000-8000-000000003304")
_INVITE = uuid.UUID("00000000-0000-4000-8000-000000003305")
_CREATED_AT = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


async def _execute(dsn: str, *statements: tuple[str, tuple[object, ...]]) -> None:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        for query, args in statements:
            await connection.execute(query, *args)
    finally:
        await connection.close()


async def _seed(dsn: str) -> None:
    statements: list[tuple[str, tuple[object, ...]]] = [
        (
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            " VALUES ($1, 'hotel-0033', 'Hotel 0033', $2, $2)",
            (_TENANT_ID, _CREATED_AT),
        )
    ]
    for user_id, role in (
        (_RECEPTION_USER, "receptionist"),
        (_STAFF_USER, "staff"),
        (_MANAGER_USER, "manager"),
    ):
        statements += [
            (
                "INSERT INTO users (id, display_name, status, is_platform_admin,"
                " created_at, updated_at) VALUES ($1, 'Сотрудник', 'active', false, $2, $2)",
                (user_id, _CREATED_AT),
            ),
            (
                "INSERT INTO tenant_memberships (id, user_id, tenant_id, role_key, status,"
                " created_at, updated_at) VALUES ($1, $2, $3, $4, 'active', $5, $5)",
                (uuid.uuid4(), user_id, _TENANT_ID, role, _CREATED_AT),
            ),
        ]
    statements.append(
        (
            "INSERT INTO staff_invites (id, tenant_id, role_key, invited_name, login,"
            " token_hash, invited_by, expires_at, created_at)"
            " VALUES ($1, $2, 'receptionist', 'Ждёт', 'WAIT', $3, $4, $5, $6)",
            (
                _INVITE,
                _TENANT_ID,
                uuid.uuid4().hex,
                _MANAGER_USER,
                _CREATED_AT + timedelta(days=3),
                _CREATED_AT,
            ),
        )
    )
    await _execute(dsn, *statements)


async def _fetch(dsn: str, query: str) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        return list(await connection.fetch(query))
    finally:
        await connection.close()


def _memberships(dsn: str, *, with_services: bool) -> dict[uuid.UUID, tuple[object, ...]]:
    columns = "user_id, role_key, service_keys" if with_services else "user_id, role_key"
    rows = asyncio.run(_fetch(dsn, f"SELECT {columns} FROM tenant_memberships"))
    return {row["user_id"]: tuple(row.values())[1:] for row in rows}


@pytest.fixture
def upgraded() -> Iterator[tuple[str, Config]]:
    with temporary_database("0032") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed(dsn))
        command.upgrade(alembic_config, "0033")
        yield dsn, alembic_config


def test_upgrade_turns_reception_role_into_service(upgraded: tuple[str, Config]) -> None:
    dsn, _ = upgraded
    assert _memberships(dsn, with_services=True) == {
        _RECEPTION_USER: ("staff", ["reception"]),
        _STAFF_USER: ("staff", []),
        _MANAGER_USER: ("manager", []),
    }
    invites = asyncio.run(_fetch(dsn, "SELECT role_key, service_keys FROM staff_invites"))
    assert [tuple(row.values()) for row in invites] == [("staff", ["reception"])]


def test_downgrade_leaves_data_the_old_image_can_read(upgraded: tuple[str, Config]) -> None:
    """Старший без ресепшена — `staff`, любой уровень со службой ресепшена —
    `receptionist`; колонок служб нет, повторный `upgrade` проходит."""
    dsn, alembic_config = upgraded
    asyncio.run(
        _execute(
            dsn,
            (
                "UPDATE tenant_memberships SET role_key = 'senior',"
                " service_keys = ARRAY['housekeeping'] WHERE user_id = $1",
                (_STAFF_USER,),
            ),
            (
                "UPDATE tenant_memberships SET role_key = 'senior',"
                " service_keys = ARRAY['housekeeping', 'reception'] WHERE user_id = $1",
                (_RECEPTION_USER,),
            ),
        )
    )

    command.downgrade(alembic_config, "0032")

    assert _memberships(dsn, with_services=False) == {
        _RECEPTION_USER: ("receptionist",),
        _STAFF_USER: ("staff",),
        _MANAGER_USER: ("manager",),
    }
    invites = asyncio.run(_fetch(dsn, "SELECT role_key FROM staff_invites"))
    assert [row["role_key"] for row in invites] == ["receptionist"]
    columns = asyncio.run(
        _fetch(
            dsn,
            "SELECT table_name FROM information_schema.columns WHERE column_name = 'service_keys'",
        )
    )
    assert columns == []
    command.upgrade(alembic_config, "0033")
    assert _memberships(dsn, with_services=True)[_RECEPTION_USER] == ("staff", ["reception"])
