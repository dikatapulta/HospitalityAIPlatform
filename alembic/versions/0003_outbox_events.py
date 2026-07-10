"""outbox доменных событий

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-10

Таблица `outbox_events` (Task 0010, P-6, ADR-005): доменное событие
публикуется в одной транзакции с бизнес-записью, воркер читает outbox и
доставляет подписчикам (at-least-once).

RLS: копия канона 0002 (тенантная сессия видит только свои события) ПЛЮС
вторая permissive-политика `platform_dispatch` — платформенная сессия (GUC
контекста тенанта пуст) читает и обновляет события ВСЕХ тенантов: диспетчер
воркера обязан забирать всю очередь. Политики одного действия объединяются
по OR. Исключение осознанное и единственное; в бизнес-таблицы оно НЕ
копируется — их канон по-прежнему 0002 (только `_apply_tenant_rls`).

Права: DML-гранты роль `hospitality_app` получает автоматически через
ALTER DEFAULT PRIVILEGES из миграции 0002 (обе миграции выполняет владелец
схемы), отдельный GRANT не нужен.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _apply_tenant_rls(table_name: str) -> None:
    """КАНОН (копия из 0002 — миграции самодостаточны и не импортируют друг друга)."""
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
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_name", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outbox_events_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(op.f("ix_outbox_events_tenant_id"), "outbox_events", ["tenant_id"])
    # Очередь диспетчера фильтруется по processed_at IS NULL; обычного btree
    # достаточно для Фазы 0 (partial-индекс — оптимизация при росте объёма).
    op.create_index(op.f("ix_outbox_events_processed_at"), "outbox_events", ["processed_at"])
    _apply_tenant_rls("outbox_events")
    op.execute(
        """
        CREATE POLICY platform_dispatch ON outbox_events
        USING (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
        WITH CHECK (NULLIF(current_setting('app.tenant_id', true), '') IS NULL)
        """
    )


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицей.
    op.drop_table("outbox_events")
