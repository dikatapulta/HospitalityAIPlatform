"""platform: uploaded_files — метаданные файлов в хранилище

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-27

Spec 0047, ADR-022 (issue #491): общий узел хранилища файлов. Байты файлов лежат
шифротекстом на томе `files_data` за портом `FileStoragePort`; здесь — только
метаданные (FOUNDATION §9). Тенантная таблица по канону 0002 (RLS ENABLE + FORCE +
политика). `guest_identity_id` без FK — ядро не ссылается на таблицы модулей.

Шаг обратно-совместим целиком: одна новая таблица, существующие не тронуты.
Downgrade удаляет таблицу; файлы на томе после этого никто не читает — их убирают
вместе с томом.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_TABLE = "uploaded_files"


def _apply_tenant_rls(table_name: str) -> None:
    """КАНОН (копия из миграции 0002 — см. обоснование в её докстринге)."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table_name}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=160), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("guest_identity_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_uploaded_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint("storage_key", name=op.f(f"uq_{_TABLE}_storage_key")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(
        "ix_uploaded_files_tenant_id_purpose_created_at",
        _TABLE,
        ["tenant_id", "purpose", "created_at"],
    )
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
