"""channels: проверки алфавита ответа гостю — guest_reply_language_checks

Revision ID: 0085
Revises: 0084
Create Date: 2026-11-12

Spec 0035 §8.2 (issue #1043 п. 1): ход гостя пишет строку на каждый ответ, у которого
и гость, и ответ написаны своим алфавитом с явным большинством; копия утренней сводки
основателю считает за сутки отеля, сколько ответов ушло не алфавитом гостя.

Тенантная по канону 0002: RLS ENABLE + FORCE + политика `tenant_isolation`; права
роли приложения — через ALTER DEFAULT PRIVILEGES миграции 0002. Текста гостя и ссылки
на диалог нет — только алфавиты и время, носителем ПД таблица не является.

Схема обратно-совместима: новая таблица, которую старый образ не читает. Бэкфилла нет —
проверок до этой миграции не было. Downgrade удаляет таблицу: теряются только числа
прошлых суток для копии сводки.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

_TABLE = "guest_reply_language_checks"


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
        sa.Column("guest_alphabet", sa.String(length=16), nullable=False),
        sa.Column("reply_alphabet", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_created_at"), _TABLE, ["created_at"])
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
