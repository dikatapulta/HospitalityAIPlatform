"""mentor: попытки экзамена «Наставника»

Revision ID: 0058
Revises: 0057
Create Date: 2026-11-03

Spec 0048 §12 (issue #779): экзамен — серия вопросов «Тренировки» с итогом в
процентах и порогом сдачи из настроек отеля. Одна тенантная таблица, RLS по
канону 0002:

- `mentor_exam_attempts` — попытка сотрудника (`user_id`, FK `users` ON DELETE
  CASCADE, как у оценок «Тренировки»): тема (глава и снимок названия или весь
  кодекс), снимок числа вопросов и порога, счёт верно / частично / неверно /
  без оценки, номера заданных статей, открытый вопрос, итог — процент и «сдан».
  Итог есть тогда и только тогда, когда попытка закончена (CHECK). Текста ответов
  в таблице нет.

FK в чужие модули нет. Шаг обратно-совместим: новая таблица, старый образ её не
видит. Downgrade удаляет попытки — после первого экзамена откат ниже 0058 стирает
результаты экзаменов отеля.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

_TABLE = "mentor_exam_attempts"


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
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=True),
        sa.Column("chapter_title", sa.Text(), nullable=True),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("pass_percent", sa.Integer(), nullable=False),
        sa.Column("correct_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("partial_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("wrong_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("ungraded_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "asked_articles",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("current_article_number", sa.String(length=16), nullable=True),
        sa.Column("current_question", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("percent", sa.Integer(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (percent IS NULL)"
            " AND (finished_at IS NULL) = (passed IS NULL)",
            name=op.f(f"ck_{_TABLE}_result_only_when_finished"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_user_id"), _TABLE, ["user_id"])
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
