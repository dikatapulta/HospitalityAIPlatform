"""documents: work_completion_acts — черновики АВР по кнопке бухгалтера

Revision ID: 0079
Revises: 0077
Create Date: 2026-11-11

Spec 0086 Р-5 (issue #953, PR 1 серии): бухгалтер формирует черновик АВР по
форме Р-1 из проверенных реквизитов запроса (`act_requests`). Таблица тенантная
по канону 0002: RLS ENABLE + FORCE + политика `tenant_isolation`; права роли
приложения — через ALTER DEFAULT PRIVILEGES миграции 0002.

Открыты номер, дата составления, сумма и кто сформировал — для строки страницы.
Снимок акта (реквизиты сторон, строки, кто проверил) — только шифртекстом
(`content_ciphertext`, BYTEA, FOUNDATION §11): в нём ИИН и ФИО ИП. Строка
обезличивается вместе со своим запросом (spec 0032): шифртекст и имя — NULL.
`act_request_id` — FK CASCADE: запрос удаляется — уходят и его акты.

Шаг обратно-совместим целиком: новая таблица, старые не тронуты, старый образ
её не читает. Бэкфилла нет — актов до этой миграции не было. Downgrade удаляет
таблицу вместе с черновиками; реквизиты запросов не трогает.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0077"
branch_labels = None
depends_on = None

_TABLE = "work_completion_acts"


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
        sa.Column("act_request_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=30), nullable=False),
        sa.Column("composed_on", sa.Date(), nullable=False),
        sa.Column("total", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("content_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_display_name", sa.String(length=255), nullable=True),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["act_request_id"],
            ["act_requests.id"],
            name=op.f(f"fk_{_TABLE}_act_request_id_act_requests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_act_request_id"), _TABLE, ["act_request_id"])
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
