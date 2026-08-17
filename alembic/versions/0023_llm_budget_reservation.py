"""ai gateway: llm_budget_reservation

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-11

Резерв дневного бюджета на время одного вызова LLM (issue #46, ADR-017):
вызов, который идёт прямо сейчас, обязан быть виден проверке бюджета до
того, как его стоимость попадёт в `llm_call_log`. Таблица тенантная —
RLS-блок `_apply_tenant_rls` скопирован из канона 0002 (ENABLE + FORCE +
политика tenant_isolation), как и в 0007 у самого журнала.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
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
        "llm_budget_reservation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("reserved_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_llm_budget_reservation_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_budget_reservation")),
    )
    op.create_index(
        op.f("ix_llm_budget_reservation_tenant_id"), "llm_budget_reservation", ["tenant_id"]
    )
    # Обе операции над таблицей — сумма живых резервов и удаление истёкших —
    # фильтруют по сроку аренды.
    op.create_index(
        op.f("ix_llm_budget_reservation_reserved_until"),
        "llm_budget_reservation",
        ["reserved_until"],
    )
    _apply_tenant_rls("llm_budget_reservation")


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table("llm_budget_reservation")
