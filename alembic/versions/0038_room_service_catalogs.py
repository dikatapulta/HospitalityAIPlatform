"""room_service: каталоги — разделы, товары и «принимаем заказы»

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-28

Spec 0038 §3 (issue #418, PR 3 серии): еда и магазин в номер — два каталога
модуля `room_service`. Три тенантные таблицы, RLS по канону 0002:

- `room_service_sections` — раздел каталога (`restaurant` | `shop`), позиция,
  названия JSONB `{"en", "ru", "kk"}`;
- `room_service_items` — товар раздела: названия, описания, цена в тиынах
  BIGINT с CHECK `price_minor > 0`, «в наличии», `hidden_at` («скрыт из меню»).
  FK на свой раздел ON DELETE RESTRICT: разделы не удаляются, а заказы (PR 5)
  ссылаются на товар;
- `room_service_settings` — PK `(tenant_id, catalog)`, `accepting_orders`
  DEFAULT false; строки нет — каталог заказов не принимает.

FK в чужие модули нет (образец `request_origins`). Шаг обратно-совместим
целиком: новые таблицы, существующие не тронуты, старый образ их не видит.
Downgrade удаляет таблицы вместе с каталогами — до PR 5 заказов на них нет;
после PR 5 откат ниже 0038 требует сначала отката заказов.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_SECTIONS = "room_service_sections"
_ITEMS = "room_service_items"
_SETTINGS = "room_service_settings"


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


def _tenant_fk(table_name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.id"],
        name=op.f(f"fk_{table_name}_tenant_id_tenants"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        _SECTIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("catalog", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _tenant_fk(_SECTIONS),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_SECTIONS}")),
    )
    op.create_index(op.f(f"ix_{_SECTIONS}_tenant_id"), _SECTIONS, ["tenant_id"])
    _apply_tenant_rls(_SECTIONS)

    op.create_table(
        _ITEMS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("descriptions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("price_minor", sa.BigInteger(), nullable=False),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price_minor > 0", name=op.f(f"ck_{_ITEMS}_price_positive")),
        _tenant_fk(_ITEMS),
        sa.ForeignKeyConstraint(
            ["section_id"],
            [f"{_SECTIONS}.id"],
            name=op.f(f"fk_{_ITEMS}_section_id_{_SECTIONS}"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_ITEMS}")),
    )
    op.create_index(op.f(f"ix_{_ITEMS}_tenant_id"), _ITEMS, ["tenant_id"])
    op.create_index(op.f(f"ix_{_ITEMS}_section_id"), _ITEMS, ["section_id"])
    _apply_tenant_rls(_ITEMS)

    op.create_table(
        _SETTINGS,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("catalog", sa.String(length=16), nullable=False),
        sa.Column("accepting_orders", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _tenant_fk(_SETTINGS),
        sa.PrimaryKeyConstraint("tenant_id", "catalog", name=op.f(f"pk_{_SETTINGS}")),
    )
    _apply_tenant_rls(_SETTINGS)


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; товары — раньше разделов (FK).
    op.drop_table(_SETTINGS)
    op.drop_table(_ITEMS)
    op.drop_table(_SECTIONS)
