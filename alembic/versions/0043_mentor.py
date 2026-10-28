"""mentor: кодекс, статьи с вектором, диалоги и оценки «Наставника»

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-28

Spec 0048 §4 (issue #492, PR 3 серии), ADR-023. «Наставник» отвечает сотруднику по
статьям кодекса отеля и ведёт «Тренировку»; диалоги и поимённая статистика живут
в базе платформы (решение основателя 23.10), а не в памяти отдельного приложения.

Пять тенантных таблиц, RLS — канон 0002 у каждой:

- `mentor_codexes` — загруженные файлы кодекса целиком, одна строка на тенанта;
- `mentor_codex_articles` — производный индекс поиска: статья и её вектор
  `vector(1536)`; поиск — `ORDER BY embedding <=> :query` без индекса (сотни строк
  на отель, HNSW — примерно с 10 тыс.);
- `mentor_conversations` — диалог сотрудника, один на `(user_id, mode)`;
- `mentor_messages` — реплики, срок — как у текстов гостя (spec 0032);
- `mentor_quiz_answers` — оценки «Тренировки» без текста ответа.

Первое использование pgvector в схеме: `CREATE EXTENSION IF NOT EXISTS vector`.
Образ `pgvector/pgvector:pg16` стоит у CI, локально и на staging; миграции идут
владельцем базы. Откат расширение не удаляет: оно ничему не мешает, а чужой
объект на нём удаление сломало бы.

Шаг безопасен и обратно-совместим: только новые таблицы, существующие не трогаются.
Старый образ в окне деплоя их просто не видит.
"""

from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_TABLES = (
    "mentor_quiz_answers",
    "mentor_messages",
    "mentor_conversations",
    "mentor_codex_articles",
    "mentor_codexes",
)


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


def _id_and_tenant(table_name: str) -> list[SchemaItem]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{table_name}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table_name}")),
    ]


def _user_fk(table_name: str, column: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["users.id"], name=op.f(f"fk_{table_name}_{column}_users"), ondelete=ondelete
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "mentor_codexes",
        *_id_and_tenant("mentor_codexes"),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        _user_fk("mentor_codexes", "uploaded_by_user_id", "SET NULL"),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_mentor_codexes_tenant_id")),
    )

    op.create_table(
        "mentor_codex_articles",
        *_id_and_tenant("mentor_codex_articles"),
        sa.Column("number", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("chapter_title", sa.Text(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1536), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "number", name=op.f("uq_mentor_codex_articles_tenant_id_number")
        ),
    )
    op.create_index(
        op.f("ix_mentor_codex_articles_tenant_id"), "mentor_codex_articles", ["tenant_id"]
    )

    op.create_table(
        "mentor_conversations",
        *_id_and_tenant("mentor_conversations"),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quiz_article_number", sa.String(length=16), nullable=True),
        sa.Column("quiz_question", sa.Text(), nullable=True),
        sa.Column(
            "quiz_recent_articles",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _user_fk("mentor_conversations", "user_id", "CASCADE"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "mode",
            name=op.f("uq_mentor_conversations_tenant_id_user_id_mode"),
        ),
    )
    op.create_index(
        op.f("ix_mentor_conversations_tenant_id"), "mentor_conversations", ["tenant_id"]
    )

    op.create_table(
        "mentor_messages",
        *_id_and_tenant("mentor_messages"),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "cited_articles",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("in_summary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["mentor_conversations.id"],
            name=op.f("fk_mentor_messages_conversation_id_mentor_conversations"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(op.f("ix_mentor_messages_tenant_id"), "mentor_messages", ["tenant_id"])
    op.create_index(
        op.f("ix_mentor_messages_conversation_id"), "mentor_messages", ["conversation_id"]
    )
    # Ретеншн (spec 0032) удаляет реплики по возрасту.
    op.create_index(op.f("ix_mentor_messages_created_at"), "mentor_messages", ["created_at"])

    op.create_table(
        "mentor_quiz_answers",
        *_id_and_tenant("mentor_quiz_answers"),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("article_number", sa.String(length=16), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("chapter_title", sa.Text(), nullable=False),
        sa.Column("grade", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _user_fk("mentor_quiz_answers", "user_id", "CASCADE"),
    )
    op.create_index(op.f("ix_mentor_quiz_answers_tenant_id"), "mentor_quiz_answers", ["tenant_id"])
    op.create_index(op.f("ix_mentor_quiz_answers_user_id"), "mentor_quiz_answers", ["user_id"])

    for table_name in reversed(_TABLES):
        _apply_tenant_rls(table_name)


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; порядок — от зависимых.
    for table_name in _TABLES:
        op.drop_table(table_name)
