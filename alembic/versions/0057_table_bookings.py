"""table_booking: запросы гостей на столик и ответы ресторанов

Revision ID: 0057
Revises: 0056
Create Date: 2026-11-03

Spec 0064 §3 (issue #702, PR 2 серии): бронь столика — ресторан, час и гости модуля
`table_booking`, жизненный цикл — у заявки в службе ресторана. Одна тенантная
таблица, RLS по канону 0002:

- `request_id` (UNIQUE, выбирается до заявки, Р-3 spec 0038) и отметка
  `request_created`; проживание и идентичность гостя (nullable: ретеншн обнуляет
  их в срок spec 0032); ключ кнопки «Отправить запрос» — UNIQUE
  `(tenant_id, stay_id, client_key)`;
- ресторан — ключ и снимок названий (JSONB), час брони в UTC, гостей 1–30 (CHECK),
  язык гостя;
- ответ (`confirmed | other_time | no_seats`, NULL — ждём), предложенный час —
  задан тогда и только тогда, когда `other_time` (CHECK), кто и когда ответил
  (FK `users` ON DELETE SET NULL и снимок имени);
- отметка отмены гостем и заявка «Гость отменил бронь» (UNIQUE, Р-3) с отметкой
  `cancel_request_created`.

FK в чужие модули нет (образец `room_service_orders`). Шаг обратно-совместим:
новая таблица, старый образ её не видит. Downgrade удаляет брони — после первого
живого запроса откат ниже 0057 стирает историю броней отеля.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

_TABLE = "table_bookings"


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
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_created", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stay_id", sa.Uuid(), nullable=True),
        sa.Column("guest_identity_id", sa.Uuid(), nullable=True),
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("restaurant_key", sa.String(length=32), nullable=False),
        sa.Column("restaurant_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("booking_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("guest_language", sa.String(length=2), nullable=False),
        sa.Column("answer", sa.String(length=16), nullable=True),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("answered_by_display_name", sa.String(length=255), nullable=True),
        sa.Column("cancelled_by_guest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "cancel_request_created", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "party_size BETWEEN 1 AND 30", name=op.f(f"ck_{_TABLE}_party_size_range")
        ),
        sa.CheckConstraint(
            "(answer IS NOT DISTINCT FROM 'other_time') = (proposed_at IS NOT NULL)",
            name=op.f(f"ck_{_TABLE}_proposed_at_only_for_other_time"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["answered_by_user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_answered_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint("request_id", name=op.f(f"uq_{_TABLE}_request_id")),
        sa.UniqueConstraint("cancel_request_id", name=op.f(f"uq_{_TABLE}_cancel_request_id")),
        sa.UniqueConstraint(
            "tenant_id", "stay_id", "client_key", name="uq_table_bookings_client_key"
        ),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_stay_id"), _TABLE, ["stay_id"])
    op.create_index(op.f(f"ix_{_TABLE}_created_at"), _TABLE, ["created_at"])
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
