"""showroom: запросы посетителей 3D-витрины и их контакты

Revision ID: 0060
Revises: 0059
Create Date: 2026-11-04

Spec 0069 §3 (issue #805, PR (в) серии #799): запрос из витрины — контакты и
пожелания модуля `showroom`, жизненный цикл — у заявки в службе продаж или
бронирования. Одна тенантная таблица, RLS по канону 0002:

- `request_id` (UNIQUE, выбирается до заявки, Р-3 spec 0038) и отметка
  `request_created`; ключ кнопки «Отправить запрос» — UNIQUE `(tenant_id, client_key)`;
- вид (`group | booking`), компания, контактное лицо, телефон, почта — NULL
  после обезличивания через 365 дней (`anonymized_at`);
- гостей 1–500 (CHECK), заезд и выезд (CHECK выезд позже заезда), номера с
  модели (JSONB), желаемый вид и тип номера — ключ и снимок названий (JSONB);
- пожелания, язык страницы, версия и время согласия.

FK в чужие модули нет (образец `table_bookings`). Шаг обратно-совместим: новая
таблица, старый образ её не видит. Downgrade удаляет запросы — после первого
живого запроса откат ниже 0060 стирает контакты посетителей (заявки остаются).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

_TABLE = "showroom_requests"


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
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("contact_name", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("guests_count", sa.Integer(), nullable=False),
        sa.Column("check_in", sa.Date(), nullable=False),
        sa.Column("check_out", sa.Date(), nullable=False),
        sa.Column("room_numbers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("wish_view_key", sa.String(length=64), nullable=True),
        sa.Column("wish_view_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("wish_type_key", sa.String(length=64), nullable=True),
        sa.Column("wish_type_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("wishes", sa.String(length=500), nullable=True),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("consent_version", sa.String(length=32), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "guests_count BETWEEN 1 AND 500", name=op.f(f"ck_{_TABLE}_guests_count_range")
        ),
        sa.CheckConstraint(
            "check_out > check_in", name=op.f(f"ck_{_TABLE}_check_out_after_check_in")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint("request_id", name=op.f(f"uq_{_TABLE}_request_id")),
        sa.UniqueConstraint("tenant_id", "client_key", name="uq_showroom_requests_client_key"),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_created_at"), _TABLE, ["created_at"])
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
