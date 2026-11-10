"""staff: ключи подписи сотрудника (passkey)

Revision ID: 0070
Revises: 0069
Create Date: 2026-11-10

ADR-008 §8, spec 0077 §4, issue #936 (PR 2 серии): сотрудник один раз привязывает
своё устройство, дальше каждое действие под своим именем подтверждает лицом или
отпечатком на нём. Здесь — таблица ключей WebAuthn.

- `staff_passkeys` — id ключа WebAuthn (`credential_id`, UNIQUE), открытый ключ
  COSE, счётчик подписей (uint32 — BIGINT), название устройства, сессия, которая
  привязала, отметка «владелец прочёл плашку», пометка погашения с причиной.
  Платформенный мир, вне RLS — как `staff_sessions` (ADR-008 §6): ключ
  принадлежит User; права роли приложения — через ALTER DEFAULT PRIVILEGES
  миграции 0002.

Шаг обратно-совместим: старый образ таблицы не видит, а подписи появятся только
с модулем записок (PR 3–4). Downgrade стирает таблицу: ключи придётся привязать
заново — до модуля записок ими ничего не подписано.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

_TABLE = "staff_passkeys"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("device_label", sa.String(length=60), nullable=False),
        sa.Column("bound_session_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_staff_passkeys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_passkeys")),
        sa.UniqueConstraint("credential_id", name=op.f("uq_staff_passkeys_credential_id")),
    )
    op.create_index(op.f("ix_staff_passkeys_user_id"), _TABLE, ["user_id"])


def downgrade() -> None:
    op.drop_table(_TABLE)
