"""housekeeping: housekeeping_floor_assignments — раздача этажей горничным

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-29

Spec 0053 (issue #478): старшая хаускипинга раздаёт этажи, и заявка уборки из
704 сначала зовёт горничную 7-го этажа. Строка — текущее состояние раздачи
(этаж у сотрудника), а не история: «Сохранить раздачу» заменяет строки отеля
целиком. Кто раздал, не хранится. Этаж — от 1 до 99 (CHECK; те же границы —
`FLOOR_MIN`/`FLOOR_MAX` модели).

Шаг обратно-совместим целиком: одна новая таблица, существующие не тронуты.
Старый образ в окне деплоя её не видит. Downgrade удаляет таблицу вместе с
раздачей — после повторного обновления старшая раздаст этажи заново, до этого
заявки уборки зовут всю службу, как без этажей.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

_TABLE = "housekeeping_floor_assignments"


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
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("floor", sa.SmallInteger(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("floor BETWEEN 1 AND 99", name=op.f(f"ck_{_TABLE}_floor_range")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_user_id_users"),
            ondelete="CASCADE",
        ),
        # Ключ начинается с tenant_id — отдельный индекс по нему не нужен.
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "floor", name=op.f(f"pk_{_TABLE}")),
    )
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и ограничения удаляются вместе с таблицей.
    op.drop_table(_TABLE)
