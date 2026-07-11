"""tenants: колонка config — конфигурация тенанта

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-11

Конфигурация тенанта (Task 0011, FOUNDATION §6): JSONB со `schema_version`
в корне; форму гарантирует Pydantic-схема `TenantConfig`
(`hospitality/platform/config.py`) — колонка заполняется только через
`store_tenant_config`. `NULL` = тенант создан, но онбординг не завершён
(например, служебный `demo-smoke` из publish_demo_event).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "config")
