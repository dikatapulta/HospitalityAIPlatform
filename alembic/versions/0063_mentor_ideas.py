"""mentor: «Предложить идею» — закрытые диалоги и карточки идей

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-07

Spec 0071 §8 (issue #871), PR 2:

- `mentor_conversations.closed_at` — отправленная идея закрывает свой диалог, и
  следующая идея начинает новый. Уникальность `(tenant_id, user_id, mode)`
  становится частичным индексом `WHERE closed_at IS NULL`: открытый диалог режима
  по-прежнему один, закрытых — сколько угодно. У `ask` и `quiz` диалог не
  закрывается — их поведение то же.
- `mentor_ideas` — карточка идеи или итог интервью, одна на диалог (FK с каскадом
  на `users` и на диалог): пересказ, пометка ИИ и её пояснение (у интервью пометки
  нет — CHECK), `sent_at` (пусто — черновик), `reviewed_at`. RLS по канону 0002.

Шаг не вполне обратно-совместим: старый образ вставляет диалог через `ON CONFLICT
(tenant_id, user_id, mode)` без предиката, а частичный индекс Postgres так не
находит — чат «Наставника» у старого app отвечает 500 от миграции до перезапуска
app тем же деплоем (секунды; на гостевой копии раздел выключен).

Downgrade стирает идеи и интервью целиком — диалоги режимов `idea` и `interview` с
репликами и карточки: старый код этих режимов не знает, а уникальность без
предиката не встанет на несколько закрытых диалогов одного сотрудника.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_CONVERSATIONS = "mentor_conversations"
_IDEAS = "mentor_ideas"
_OLD_UNIQUE = "uq_mentor_conversations_tenant_id_user_id_mode"
_OPEN_UNIQUE = "uq_mentor_conversations_open_per_mode"


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


def _backfill_tenant_table(sql: str) -> None:
    """CANONICAL (копия из миграции 0002 — см. обоснование в её докстринге)."""
    op.execute("SET LOCAL row_security = off")
    op.execute(sql)
    op.execute("SET LOCAL row_security = on")


def upgrade() -> None:
    op.add_column(_CONVERSATIONS, sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.drop_constraint(_OLD_UNIQUE, _CONVERSATIONS, type_="unique")
    op.create_index(
        _OPEN_UNIQUE,
        _CONVERSATIONS,
        ["tenant_id", "user_id", "mode"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        _IDEAS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("mark", sa.String(length=16), nullable=True),
        sa.Column("mark_note", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind = 'idea' OR (mark IS NULL AND mark_note IS NULL)",
            name=op.f(f"ck_{_IDEAS}_interview_has_no_mark"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_IDEAS}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{_IDEAS}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{_CONVERSATIONS}.id"],
            name=op.f(f"fk_{_IDEAS}_conversation_id_{_CONVERSATIONS}"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_IDEAS}")),
        sa.UniqueConstraint("conversation_id", name=op.f(f"uq_{_IDEAS}_conversation_id")),
    )
    op.create_index(op.f(f"ix_{_IDEAS}_tenant_id"), _IDEAS, ["tenant_id"])
    op.create_index(op.f(f"ix_{_IDEAS}_user_id"), _IDEAS, ["user_id"])
    _apply_tenant_rls(_IDEAS)


def downgrade() -> None:
    # Политика, индексы и FK карточек удаляются вместе с таблицей.
    op.drop_table(_IDEAS)
    # Реплики уходят каскадом. Под ролью без обхода RLS DELETE удалил бы 0 строк
    # молча, и уникальность ниже упала бы на чужих данных — канон 0002 делает отказ громким.
    _backfill_tenant_table(
        f"DELETE FROM {_CONVERSATIONS} WHERE mode IN ('idea', 'interview') OR closed_at IS NOT NULL"
    )
    op.drop_index(_OPEN_UNIQUE, table_name=_CONVERSATIONS)
    op.create_unique_constraint(_OLD_UNIQUE, _CONVERSATIONS, ["tenant_id", "user_id", "mode"])
    op.drop_column(_CONVERSATIONS, "closed_at")
