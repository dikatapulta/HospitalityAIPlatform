"""room_service: заказы гостя и их позиции

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-28

Spec 0038 §3 (issue #418, PR 5 серии): заказ в номер — корзина и суммы модуля
`room_service`, жизненный цикл — у заявки. Две тенантные таблицы, RLS по канону 0002:

- `room_service_orders` — каталог, `request_id` (UNIQUE, выбирается до заявки,
  Р-3) и отметка `request_created`, проживание и идентичность гостя (nullable:
  ретеншн обнуляет их через 90 дней), ключ кнопки «Оформить» — UNIQUE
  `(tenant_id, stay_id, client_order_key)`, язык, валюта, суммы в тиынах BIGINT,
  процент сбора, причина отмены;
- `room_service_order_lines` — снимок названия и цены, количество 1–20 (CHECK),
  FK на заказ ON DELETE CASCADE и на товар ON DELETE RESTRICT.

FK в чужие модули нет (образец `request_origins`). Шаг обратно-совместим:
новые таблицы, старый образ их не видит. Downgrade удаляет заказы — после
первого живого заказа откат ниже 0044 стирает историю заказов отеля.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_ORDERS = "room_service_orders"
_LINES = "room_service_order_lines"


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
        _ORDERS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("catalog", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_created", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stay_id", sa.Uuid(), nullable=True),
        sa.Column("guest_identity_id", sa.Uuid(), nullable=True),
        sa.Column("client_order_key", sa.String(length=64), nullable=False),
        sa.Column("guest_language", sa.String(length=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("service_charge_percent", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("service_charge_minor", sa.BigInteger(), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("cancel_reason", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _tenant_fk(_ORDERS),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_ORDERS}")),
        sa.UniqueConstraint("request_id", name=op.f(f"uq_{_ORDERS}_request_id")),
        sa.UniqueConstraint(
            "tenant_id", "stay_id", "client_order_key", name="uq_room_service_orders_client_key"
        ),
    )
    op.create_index(op.f(f"ix_{_ORDERS}_tenant_id"), _ORDERS, ["tenant_id"])
    op.create_index(op.f(f"ix_{_ORDERS}_stay_id"), _ORDERS, ["stay_id"])
    op.create_index(op.f(f"ix_{_ORDERS}_created_at"), _ORDERS, ["created_at"])
    _apply_tenant_rls(_ORDERS)

    op.create_table(
        _LINES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total_minor", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("quantity BETWEEN 1 AND 20", name=op.f(f"ck_{_LINES}_quantity_range")),
        sa.CheckConstraint("unit_price_minor > 0", name=op.f(f"ck_{_LINES}_unit_price_positive")),
        _tenant_fk(_LINES),
        sa.ForeignKeyConstraint(
            ["order_id"],
            [f"{_ORDERS}.id"],
            name=op.f(f"fk_{_LINES}_order_id_{_ORDERS}"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["room_service_items.id"],
            name=op.f(f"fk_{_LINES}_item_id_room_service_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_LINES}")),
    )
    op.create_index(op.f(f"ix_{_LINES}_tenant_id"), _LINES, ["tenant_id"])
    op.create_index(op.f(f"ix_{_LINES}_order_id"), _LINES, ["order_id"])
    op.create_index(op.f(f"ix_{_LINES}_item_id"), _LINES, ["item_id"])
    _apply_tenant_rls(_LINES)


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; позиции — раньше заказов (FK).
    op.drop_table(_LINES)
    op.drop_table(_ORDERS)
