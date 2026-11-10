"""mentor: пробная попытка на проекте кодекса и отметки «Неточность в статье»

Revision ID: 0069
Revises: 0067
Create Date: 2026-11-10

Spec 0072 §6 (issue #898): проект — вариант (в), «Неточность» — (б):

- `mentor_exam_attempts.on_draft_codex` — попытка начата, когда кодекс был проектом
  (`Статус: проект` в шапке): она пробная и в итог по назначению не идёт. Снимок при начале,
  как порог: утверждённая редакция прошлых итогов не перепишет. Прежние попытки — `false`
  (server_default): статус кодекса на момент их начала не сохранился.
- `mentor_article_flags` — новая тенантная таблица, RLS по канону 0002: сотрудник отметил
  «Неточность в статье» (одна отметка на статью, без текста). Номер статьи — строкой, как
  `mentor_quiz_answers.article_number`; отметки статей, текст которых сменила новая редакция,
  снимает загрузка кодекса. Строка `users` удалена — отметки уходят с ней (FK CASCADE).

Шаг обратно-совместим: старый образ новую колонку не пишет (server_default), новой таблицы
не видит. Downgrade стирает отметки и пометку попыток — после отката ниже 0069 пробные попытки
становятся зачётными, а счётчики «Неточностей» пропадают. server_default колонки остаётся и
после upgrade: старый образ попытку пишет без неё.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0067"
branch_labels = None
depends_on = None

_FLAGS = "mentor_article_flags"
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
    op.add_column(
        _ATTEMPTS,
        sa.Column("on_draft_codex", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        _FLAGS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("article_number", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_FLAGS}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{_FLAGS}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_FLAGS}")),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "article_number",
            name="uq_mentor_article_flags_tenant_id_user_id_article_number",
        ),
    )
    op.create_index(op.f(f"ix_{_FLAGS}_tenant_id"), _FLAGS, ["tenant_id"])
    op.create_index(op.f(f"ix_{_FLAGS}_user_id"), _FLAGS, ["user_id"])
    _apply_tenant_rls(_FLAGS)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_FLAGS)
    op.drop_column(_ATTEMPTS, "on_draft_codex")
