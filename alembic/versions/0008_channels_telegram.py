"""channels telegram: conversations, messages

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-13

Таблицы канала Telegram (Task 0016, §9). Обе тенантные: RLS-блок
`_apply_tenant_rls` скопирован из канона 0002 (ENABLE + FORCE + политика
tenant_isolation) — как предписывает его докстринг.

Колонки `direction`/`content_kind` — VARCHAR(16) со значениями enum'ов модели
(channels/telegram/models.py): непустой native enum Postgres намеренно не
заводится — изменение состава значений остаётся миграцией данных, а не ALTER TYPE.

Идемпотентность вебхука (P-8) держит уникальное ограничение
`messages(tenant_id, idempotency_key)`; исходящие сообщения имеют NULL-ключ
(Postgres считает NULL-и различными — ограничение их не затрагивает).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
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
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_conversations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint(
            "tenant_id", "channel", "external_id", name=op.f("uq_conversations_tenant_id")
        ),
    )
    op.create_index(op.f("ix_conversations_tenant_id"), "conversations", ["tenant_id"])
    _apply_tenant_rls("conversations")

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("content_kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("external_message_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_messages_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name=op.f("uq_messages_tenant_id")),
    )
    op.create_index(op.f("ix_messages_tenant_id"), "messages", ["tenant_id"])
    op.create_index(op.f("ix_messages_conversation_id"), "messages", ["conversation_id"])
    _apply_tenant_rls("messages")


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; порядок — из-за FK.
    op.drop_table("messages")
    op.drop_table("conversations")
