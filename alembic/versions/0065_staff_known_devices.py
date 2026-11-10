"""staff: знакомые устройства входа кабинета

Revision ID: 0065
Revises: 0063
Create Date: 2026-11-10

Spec 0033 §3.3, issue #402: браузер, где по логину уже входили с верным
паролем, входит мимо ключей rate-limit учётки и IP — их гость исчерпывал сам
(код отеля публичен, логин с бейджа, адрес у отеля с гостевым Wi-Fi общий).

- `staff_known_devices` — пара «SHA-256 токена из cookie `staff_device` +
  учётка», когда она входила последней. Платформенный мир, вне RLS — как
  `staff_sessions` (ADR-008 §6); права роли приложения — через ALTER DEFAULT
  PRIVILEGES миграции 0002.

Шаг обратно-совместим: старый образ таблицы не видит, а его вход — прежний
строгий путь всех ключей. Downgrade стирает таблицу: после возврата на новый
образ каждый браузер станет знакомым заново первым же входом.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0063"
branch_labels = None
depends_on = None

_TABLE = "staff_known_devices"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_staff_known_devices_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_known_devices")),
    )
    op.create_index(op.f("ix_staff_known_devices_user_id"), _TABLE, ["user_id"])
    op.create_index(
        "uq_staff_known_devices_token_hash_user_id",
        _TABLE,
        ["token_hash", "user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
