"""mentor: назначенный экзамен «Наставника»

Revision ID: 0067
Revises: 0065
Create Date: 2026-11-10

Spec 0072 §3 (issue #898): менеджер назначает экзамен — тему (весь кодекс или главы)
всем или службам, а «Итоги экзаменов» считают только назначенный.

- `mentor_exam_assignments` — новая тенантная таблица, RLS по канону 0002: снимок глав
  `{number, title}` массивом (пустой — весь кодекс), ключи служб (пустой — всем), автор
  (FK `users` ON DELETE SET NULL, как `mentor_codexes.uploaded_by_user_id`), когда
  назначен и когда снят. Данных о человеке, кроме автора, нет.
- `mentor_exam_attempts.assignment_id` — FK на назначение ON DELETE SET NULL; `NULL` —
  экзамен для себя. Колонка новая и пустая: бэкфилла нет, прежние попытки — «для себя».

Шаг обратно-совместим: старый образ новой таблицы не видит, а новую колонку не пишет
(`NULL`). Downgrade стирает назначения и ссылку на них — после первого назначения откат
ниже 0067 превращает назначенные попытки в экзамены для себя, «Итоги» по назначениям
пропадают.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067"
down_revision = "0065"
branch_labels = None
depends_on = None

_TABLE = "mentor_exam_assignments"
_ATTEMPTS = "mentor_exam_attempts"


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
        sa.Column("chapters", postgresql.JSONB(), nullable=False),
        sa.Column("service_keys", postgresql.JSONB(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
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
    _apply_tenant_rls(_TABLE)

    op.add_column(_ATTEMPTS, sa.Column("assignment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{_ATTEMPTS}_assignment_id_{_TABLE}"),
        _ATTEMPTS,
        _TABLE,
        ["assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f(f"ix_{_ATTEMPTS}_assignment_id"), _ATTEMPTS, ["assignment_id"])


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_ATTEMPTS}_assignment_id"), table_name=_ATTEMPTS)
    op.drop_constraint(
        op.f(f"fk_{_ATTEMPTS}_assignment_id_{_TABLE}"), _ATTEMPTS, type_="foreignkey"
    )
    op.drop_column(_ATTEMPTS, "assignment_id")
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
