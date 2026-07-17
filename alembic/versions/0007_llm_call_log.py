"""ai gateway: llm_call_log

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-13

Журнал вызовов LLM через AI Gateway (Task 0014, FOUNDATION §7.2): тенант,
correlation id, хэш промпта, токены, стоимость, latency, статус. Таблица
тенантная: RLS-блок `_apply_tenant_rls` скопирован из канона 0002
(ENABLE + FORCE + политика tenant_isolation).

Колонка `status` — VARCHAR(16) со значениями `LlmCallStatus` (модель
gateway): native enum Postgres не заводится по канону модуля requests.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
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
        "llm_call_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_llm_call_log_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_call_log")),
    )
    op.create_index(op.f("ix_llm_call_log_tenant_id"), "llm_call_log", ["tenant_id"])
    # Бюджетный запрос gateway: сумма cost_usd тенанта за текущие UTC-сутки.
    op.create_index(op.f("ix_llm_call_log_created_at"), "llm_call_log", ["created_at"])
    _apply_tenant_rls("llm_call_log")


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table("llm_call_log")
