"""feedback: оценка гостем выполненной заявки и связь сообщения с заявкой

Revision ID: 0068
Revises: 0067
Create Date: 2026-11-10

Spec 0076 §4.1 (issue #899), PR 1 серии:

- `request_feedback` — одна оценка на заявку (`up` / `down`), уникальность
  `(tenant_id, request_id)`: на ней стоит `ON CONFLICT DO NOTHING` двойного касания.
  Комментарий гостя, его время и карточка пальца вниз — nullable. RLS по канону 0002.
- `messages.request_id` — заявка, итог которой сообщает исходящее: под ним веб-чат
  ставит кнопки оценки. Nullable, без FK (граница модулей), без бэкфилла: прежние
  итоги кнопок не получат.

Обратно-совместим: старый образ новой таблицы не видит, колонку `messages` не пишет.
Мешают ему только данные — заявки `origin = 'feedback'` (рунбук deploy.md, строка #899).
Downgrade стирает оценки и комментарии целиком.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

_TABLE = "request_feedback"


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
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("comment_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("card_request_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint(
            "tenant_id", "request_id", name=op.f(f"uq_{_TABLE}_tenant_id_request_id")
        ),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_created_at"), _TABLE, ["created_at"])
    _apply_tenant_rls(_TABLE)

    op.add_column("messages", sa.Column("request_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "request_id")
    # Политика, индексы и FK удаляются вместе с таблицей.
    op.drop_table(_TABLE)
