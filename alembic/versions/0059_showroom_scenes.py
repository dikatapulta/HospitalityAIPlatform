"""showroom: сцена 3D-витрины

Revision ID: 0059
Revises: 0058
Create Date: 2026-11-04

Spec 0069 §3 (issue #803, PR а серии #799): сцена витрины — здание отеля числами,
один документ JSONB на тенанта. Одна тенантная таблица, RLS по канону 0002:

- `showroom_scenes` — `tenant_id` UNIQUE (одна сцена на отель, новая заменяет
  прежнюю), `scene` JSONB (`SceneDocument`), `scene_version`, кто и когда заменил
  (`replaced_by_user_id`, FK `users` ON DELETE SET NULL). ПД нет.

FK в чужие модули нет. Шаг обратно-совместим: новая таблица, старый образ её не
видит. Downgrade удаляет сцены — после загрузки откат ниже 0059 стирает сцену
отеля (файл у менеджера остаётся, загрузить заново).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

_TABLE = "showroom_scenes"


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
        sa.Column("scene", postgresql.JSONB(), nullable=False),
        sa.Column("scene_version", sa.Integer(), nullable=False),
        sa.Column("replaced_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_replaced_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint("tenant_id", name=op.f(f"uq_{_TABLE}_tenant_id")),
    )
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и ограничения удаляются вместе с таблицей.
    op.drop_table(_TABLE)
