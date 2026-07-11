"""requests module: request_categories, service_requests

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-12

Таблицы канонического доменного модуля requests (Task 0012, FOUNDATION §5.2).
Обе тенантные: RLS-блок `_apply_tenant_rls` скопирован из канона 0002
(ENABLE + FORCE + политика tenant_isolation) — как предписывает его докстринг.

Колонка `status` — VARCHAR(32) со значениями `RequestStatus` (модель requests):
непустой native enum Postgres намеренно не заводится — изменение состава
статусов остаётся обычной миграцией данных, а не ALTER TYPE (см. models.py).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


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
        "request_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_request_categories_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_categories")),
        sa.UniqueConstraint("tenant_id", "key", name=op.f("uq_request_categories_tenant_id")),
    )
    op.create_index(op.f("ix_request_categories_tenant_id"), "request_categories", ["tenant_id"])
    _apply_tenant_rls("request_categories")

    op.create_table(
        "service_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("room_number", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_service_requests_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["request_categories.id"],
            name=op.f("fk_service_requests_category_id_request_categories"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_requests")),
    )
    op.create_index(op.f("ix_service_requests_tenant_id"), "service_requests", ["tenant_id"])
    op.create_index(op.f("ix_service_requests_category_id"), "service_requests", ["category_id"])
    _apply_tenant_rls("service_requests")


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; порядок — из-за FK.
    op.drop_table("service_requests")
    op.drop_table("request_categories")
