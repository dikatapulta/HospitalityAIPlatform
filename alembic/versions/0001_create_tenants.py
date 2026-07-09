"""create tenants

Revision ID: 0001
Revises:
Create Date: 2026-07-09

Миграции не импортируют код приложения (модели меняются, история миграций —
нет), поэтому типы здесь сырые SQLAlchemy: `sa.DateTime(timezone=True)` — это
DDL-эквивалент канонического `UTCDateTime` из моделей.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )


def downgrade() -> None:
    op.drop_table("tenants")
