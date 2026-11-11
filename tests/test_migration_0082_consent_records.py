"""Миграция 0082 — `consent_records` (spec 0032 §6, issue #948).

Канон `test_migration_0072`: БД до `0073`, строки двух живых отелей, шаг вперёд.
Предмет — бэкфилл: согласие на сессии получает запись с комнатой и датами её
Stay, согласие на диалоге — запись без них, диалог без согласия — ничего; у
каждого отеля свои строки. Затем шаг назад и снова вперёд.
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

_HOTEL_A = uuid.UUID("00000000-0000-4000-8000-000000008201")
_HOTEL_B = uuid.UUID("00000000-0000-4000-8000-000000008202")
_NOW = datetime.now(UTC).replace(microsecond=0)
_CHECK_IN = _NOW - timedelta(days=2)
_CHECK_OUT = _NOW + timedelta(days=1)
_SESSION_CONSENT = _NOW - timedelta(days=1)
_DIALOG_CONSENT = _NOW - timedelta(hours=3)
_VERSION = "2026-07-29-v3"


async def _seed_hotel(
    connection: asyncpg.Connection, tenant_id: uuid.UUID, *, slug: str, kind: str, room: str
) -> None:
    """Отель с одним проживанием, привязанным гостем и его сессией."""
    guest_id, identity_id, stay_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await connection.execute(
        "INSERT INTO tenants (id, slug, name, created_at, updated_at) VALUES ($1, $2, $2, $3, $3)",
        tenant_id,
        slug,
        _NOW,
    )
    await connection.execute(
        "INSERT INTO guests (id, tenant_id, display_name, created_at, updated_at)"
        " VALUES ($1, $2, 'Wang Li', $3, $3)",
        guest_id,
        tenant_id,
        _CHECK_IN,
    )
    await connection.execute(
        "INSERT INTO guest_identities (id, tenant_id, guest_id, kind, external_id, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        identity_id,
        tenant_id,
        guest_id,
        kind,
        uuid.uuid4().hex,
        _CHECK_IN,
    )
    await connection.execute(
        "INSERT INTO stays (id, tenant_id, guest_id, room_number, status, guests_count,"
        " check_in_at, check_out_at, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, 'checked_in', 1, $5, $6, $5, $5)",
        stay_id,
        tenant_id,
        guest_id,
        room,
        _CHECK_IN,
        _CHECK_OUT,
    )
    await connection.execute(
        "INSERT INTO guest_sessions (id, tenant_id, stay_id, guest_identity_id, token_hash,"
        " last_used_at, consent_at, consent_version, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $6)",
        uuid.uuid4(),
        tenant_id,
        stay_id,
        identity_id,
        uuid.uuid4().hex,
        _SESSION_CONSENT,
        _VERSION,
    )


async def _seed_dialog(
    connection: asyncpg.Connection, tenant_id: uuid.UUID, *, consented: bool
) -> None:
    await connection.execute(
        "INSERT INTO conversations (id, tenant_id, channel, external_id, consent_at,"
        " consent_version, created_at, updated_at) VALUES ($1, $2, 'telegram', $3, $4, $5, $6, $6)",
        uuid.uuid4(),
        tenant_id,
        uuid.uuid4().hex,
        _DIALOG_CONSENT if consented else None,
        _VERSION if consented else None,
        _NOW,
    )


async def _seed(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        await _seed_hotel(connection, _HOTEL_A, slug="hotel-0082-a", kind="web", room="305")
        await _seed_hotel(connection, _HOTEL_B, slug="hotel-0082-b", kind="telegram", room="512")
        await _seed_dialog(connection, _HOTEL_A, consented=True)
        await _seed_dialog(connection, _HOTEL_A, consented=False)
    finally:
        await connection.close()


async def _records(dsn: str) -> set[tuple[object, ...]]:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        rows = await connection.fetch(
            "SELECT tenant_id, channel, consent_at, consent_version, room_number,"
            " check_in_at, check_out_at FROM consent_records"
        )
    finally:
        await connection.close()
    return {tuple(row.values()) for row in rows}


_EXPECTED = {
    (_HOTEL_A, "web", _SESSION_CONSENT, _VERSION, "305", _CHECK_IN, _CHECK_OUT),
    (_HOTEL_A, "telegram", _DIALOG_CONSENT, _VERSION, None, None, None),
    (_HOTEL_B, "telegram", _SESSION_CONSENT, _VERSION, "512", _CHECK_IN, _CHECK_OUT),
}


@pytest.fixture
def upgraded() -> Iterator[tuple[str, Config]]:
    with temporary_database("0073") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed(dsn))
        command.upgrade(alembic_config, "0082")
        yield dsn, alembic_config


def test_existing_consents_get_records(upgraded: tuple[str, Config]) -> None:
    dsn, _ = upgraded
    assert asyncio.run(_records(dsn)) == _EXPECTED


def test_downgrade_and_upgrade_again(upgraded: tuple[str, Config]) -> None:
    dsn, alembic_config = upgraded
    command.downgrade(alembic_config, "0073")
    command.upgrade(alembic_config, "0082")
    assert asyncio.run(_records(dsn)) == _EXPECTED
