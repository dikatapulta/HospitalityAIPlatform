"""platform: staff_action_log — журнал изменений кабинета

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-16

Spec 0041 (issue #370): менеджер видит, кто из сотрудников что сделал с
заявками и заселением. Тенантная таблица ядра по канону `TenantIsolationCanary`
и RLS-блок по канону 0002. `request_id`/`stay_id` без внешних ключей — ядро не
ссылается на таблицы модулей; `actor_user_id` — FK на платформенную `users`
с SET NULL, как `claimed_by_user_id` заявки. Текстов гостя в таблице нет по
построению; логин сотрудника — PII (docs/PII_REGISTRY.md), срок — как у
текстов гостей (spec 0032), удаляет джоба ретеншна.

Шаг обратно-совместим целиком: одна новая таблица, ни одной правки
существующих. Старый образ в окне деплоя её не видит и строк не пишет.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
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
        "staff_action_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_login", sa.String(length=12), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("stay_id", sa.Uuid(), nullable=True),
        sa.Column("request_daily_number", sa.Integer(), nullable=True),
        sa.Column("request_service_day", sa.Date(), nullable=True),
        sa.Column("category_name", sa.String(length=255), nullable=True),
        sa.Column("room_number", sa.String(length=20), nullable=True),
        sa.Column("room_number_to", sa.String(length=20), nullable=True),
        sa.Column("nights", sa.SmallInteger(), nullable=True),
        sa.Column("with_note", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_staff_action_log_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_staff_action_log_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_action_log")),
    )
    op.create_index(op.f("ix_staff_action_log_tenant_id"), "staff_action_log", ["tenant_id"])
    # Чтение журнала — день тенанта, новые сверху; ретеншн — по occurred_at.
    op.create_index(
        "ix_staff_action_log_tenant_id_occurred_at",
        "staff_action_log",
        ["tenant_id", sa.literal_column("occurred_at DESC")],
    )
    _apply_tenant_rls("staff_action_log")


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table("staff_action_log")
