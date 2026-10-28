"""Миграция 0028 — вход по логину отеля, email уходит (spec 0037 §6, issue #368).

Проверяется поведением, а не формой файла (канон `test_migration_0025`):
«доисторическая» БД поднимается до `0027`, наполняется строками сырым SQL, как
их видел бы живой отель на момент выкатки, и только потом делается шаг на
`0028`. Предмет — что стало со СТАРЫМИ строками:

- email-идентичности удалены — цель задачи не хранить email;
- пользователи и членства на месте — история «кто взял / кто закрыл»;
- ожидающие приглашения без логина отозваны, принятое и уже истёкшее не
  тронуты;
- upgrade → downgrade → upgrade не сносит уже выданные логины: условие
  удаления — `@` в `external_id`, а не «все `password`».

Файл лежит в `tests/`, а не рядом с модулем: проверяется шаг схемы, общий для
всей БД. Таблицы здесь платформенные (вне RLS, ADR-008 §6), поэтому сырой
доступ ролью-владельцем ничего не обходит — но образцом для модульных тестов
он всё равно не служит (см. докстринг `test_migration_0025`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.conftest import database_dsn, temporary_database

_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000101")
_EMAIL_USER = uuid.UUID("00000000-0000-4000-8000-000000000201")
_LOGIN_USER = uuid.UUID("00000000-0000-4000-8000-000000000202")
_PENDING_INVITE = uuid.UUID("00000000-0000-4000-8000-000000000301")
_ACCEPTED_INVITE = uuid.UUID("00000000-0000-4000-8000-000000000302")
_EXPIRED_INVITE = uuid.UUID("00000000-0000-4000-8000-000000000303")

_CREATED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
_FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
_LOGIN_EXTERNAL_ID = f"{_TENANT_ID}:BORM"


async def _seed_pre_migration_rows(dsn: str) -> None:
    """Отель в схеме 0027: учётка с email, учётка с логином (её мог выдать
    новый образ до отката схемы), три приглашения — ожидающее, принятое,
    истёкшее."""
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        await connection.execute(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            " VALUES ($1, 'hotel-pre-0028', 'Hotel Pre 0028', $2, $2)",
            _TENANT_ID,
            _CREATED_AT,
        )
        for user_id, external_id in (
            (_EMAIL_USER, "manager@hotel.kz"),
            (_LOGIN_USER, _LOGIN_EXTERNAL_ID),
        ):
            await connection.execute(
                "INSERT INTO users (id, display_name, status, is_platform_admin,"
                " created_at, updated_at) VALUES ($1, 'Сотрудник', 'active', false, $2, $2)",
                user_id,
                _CREATED_AT,
            )
            await connection.execute(
                "INSERT INTO user_identities (id, user_id, kind, external_id, secret_hash,"
                " created_at) VALUES ($1, $2, 'password', $3, 'hash', $4)",
                uuid.uuid4(),
                user_id,
                external_id,
                _CREATED_AT,
            )
            await connection.execute(
                "INSERT INTO tenant_memberships (id, user_id, tenant_id, role_key, status,"
                " created_at, updated_at) VALUES ($1, $2, $3, 'manager', 'active', $4, $4)",
                uuid.uuid4(),
                user_id,
                _TENANT_ID,
                _CREATED_AT,
            )
        for invite_id, expires_at, accepted_at in (
            (_PENDING_INVITE, _FAR_FUTURE, None),
            (_ACCEPTED_INVITE, _FAR_FUTURE, _CREATED_AT),
            (_EXPIRED_INVITE, _CREATED_AT + timedelta(hours=72), None),
        ):
            await connection.execute(
                "INSERT INTO staff_invites (id, tenant_id, role_key, invited_name, token_hash,"
                " invited_by, expires_at, accepted_at, created_at)"
                " VALUES ($1, $2, 'staff', 'Приглашённый', $3, $4, $5, $6, $7)",
                invite_id,
                _TENANT_ID,
                uuid.uuid4().hex,
                _EMAIL_USER,
                expires_at,
                accepted_at,
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


@dataclass(frozen=True)
class UpgradedDatabase:
    """БД сразу после шага 0027 → 0028 — предмет каждого теста ниже."""

    dsn: str
    alembic_config: Config

    def fetch(self, query: str) -> list[asyncpg.Record]:
        return asyncio.run(_fetch(self.dsn, query))


@pytest.fixture
def upgraded() -> Iterator[UpgradedDatabase]:
    """Поднять БД до 0027, наполнить доисторическими строками, шагнуть на 0028."""
    with temporary_database("0027") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed_pre_migration_rows(dsn))
        command.upgrade(alembic_config, "0028")
        yield UpgradedDatabase(dsn, alembic_config)


def test_email_identities_are_deleted_and_logins_kept(upgraded: UpgradedDatabase) -> None:
    rows = upgraded.fetch("SELECT user_id, external_id FROM user_identities")
    assert [(row["user_id"], row["external_id"]) for row in rows] == [
        (_LOGIN_USER, _LOGIN_EXTERNAL_ID)
    ]


def test_users_and_memberships_stay(upgraded: UpgradedDatabase) -> None:
    """История «кто взял / кто закрыл» атрибутирована по user_id — её не трогаем."""
    users = {row["id"] for row in upgraded.fetch("SELECT id FROM users")}
    memberships = upgraded.fetch("SELECT user_id, status FROM tenant_memberships")
    assert users == {_EMAIL_USER, _LOGIN_USER}
    assert {(row["user_id"], row["status"]) for row in memberships} == {
        (_EMAIL_USER, "active"),
        (_LOGIN_USER, "active"),
    }


def test_pending_invites_without_login_are_revoked(upgraded: UpgradedDatabase) -> None:
    """Принять приглашение без логина нечем — миграция его отзывает; принятое
    и истёкшее не трогает (их `expires_at` — история, а не состояние)."""
    rows = {
        row["id"]: row
        for row in upgraded.fetch("SELECT id, login, expires_at, accepted_at FROM staff_invites")
    }
    assert all(row["login"] is None for row in rows.values())
    # Сравнение с исходным сроком, а не с часами хоста: `now()` пишет база.
    assert rows[_PENDING_INVITE]["expires_at"] < _FAR_FUTURE
    assert rows[_ACCEPTED_INVITE]["expires_at"] == _FAR_FUTURE
    assert rows[_EXPIRED_INVITE]["expires_at"] == _CREATED_AT + timedelta(hours=72)


def test_downgrade_and_upgrade_again_keep_issued_logins(upgraded: UpgradedDatabase) -> None:
    """`downgrade` возвращает только схему (email восстановить неоткуда), а
    повторный `upgrade` не сносит уже выданные логины."""
    command.downgrade(upgraded.alembic_config, "0027")
    columns = {
        row["column_name"]
        for row in upgraded.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'staff_invites'"
        )
    }
    assert "login" not in columns

    command.upgrade(upgraded.alembic_config, "0028")
    rows = upgraded.fetch("SELECT external_id FROM user_identities")
    assert [row["external_id"] for row in rows] == [_LOGIN_EXTERNAL_ID]
