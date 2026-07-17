"""walking skeleton: conversations.pending_action, request_origins

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-13

Сквозная сборка (Task 0017, ADR-011). Два изменения композиционного слоя канала
Telegram; доменные таблицы не трогаются.

- `conversations.pending_action` (JSONB, NULL) — состояние гейта подтверждения P-9
  между ходами диалога (сериализованный `PendingAction`).
- `request_origins` — тенантная таблица привязки «заявка → диалог-источник» для
  доставки гостю подтверждения о выполнении. RLS-блок `_apply_tenant_rls`
  скопирован из канона 0002 (ENABLE + FORCE + политика tenant_isolation).
  `request_id` — без FK на service_requests (канал не связывает свою схему с
  таблицей чужого модуля, P-2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
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
    op.add_column(
        "conversations",
        sa.Column("pending_action", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "request_origins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_request_origins_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_request_origins_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_origins")),
        sa.UniqueConstraint("tenant_id", "request_id", name=op.f("uq_request_origins_tenant_id")),
    )
    op.create_index(op.f("ix_request_origins_tenant_id"), "request_origins", ["tenant_id"])
    op.create_index(
        op.f("ix_request_origins_conversation_id"), "request_origins", ["conversation_id"]
    )
    _apply_tenant_rls("request_origins")


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицей.
    op.drop_table("request_origins")
    op.drop_column("conversations", "pending_action")
