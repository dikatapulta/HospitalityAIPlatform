"""guests: stay_bind_links — ссылка привязки в Postgres, срок — от Stay

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-21

Issue #354, spec 0033 §6, уточнение ADR-008 §3: QR привязки печатается на
талоне и отдаётся гостю вместе с ключом. Прежняя ссылка жила в Redis 120 с и
гасла первым сканированием; для бумаги неверно всё — срок, одноразовость и
само хранилище: Redis staging без сохранения на диск, и его перезапуск молча
погасил бы каждый напечатанный талон. Поэтому своя тенантная таблица: в ней
только SHA-256 токена (канон `guest_sessions.token_hash`), срок жизни
производен от Stay и в строке не хранится.

Шаг обратно-совместим целиком: одна новая таблица, существующие не тронуты.
Ссылки, выпущенные старым образом в Redis, после деплоя недействительны —
они и так жили две минуты.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
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
        "stay_bind_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("stay_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_stay_bind_links_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stay_id"],
            ["stays.id"],
            name=op.f("fk_stay_bind_links_stay_id_stays"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stay_bind_links")),
    )
    op.create_index(op.f("ix_stay_bind_links_tenant_id"), "stay_bind_links", ["tenant_id"])
    op.create_index(op.f("ix_stay_bind_links_stay_id"), "stay_bind_links", ["stay_id"])
    op.create_index(
        "uq_stay_bind_links_tenant_token",
        "stay_bind_links",
        ["tenant_id", "token_hash"],
        unique=True,
    )
    _apply_tenant_rls("stay_bind_links")


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table("stay_bind_links")
