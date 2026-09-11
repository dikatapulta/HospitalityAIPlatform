"""channels/common: unanswered_questions — вопросы гостей без факта в справочнике

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-10

Spec 0036 §6 (issue #334): справочник отеля заполняет отель, а знает, чего в нём
не хватает, только гость. Модель сообщает об этом служебным сигналом
`report_unanswered_question`, канал пишет сюда строку, а страница «Справочник
отеля» (PR D) показывает её менеджеру окном в 7 дней. Эскалацией это не
является (spec 0022): «во сколько завтрак» не требует человека сейчас, а отель
на 310 номеров, получающий уведомление на каждый неизвестный факт, выключит
уведомления к концу первой недели.

Устройство — копия соседней 0026 (`conversation_escalations`) с одним намеренным
отличием: `conversation_id` здесь `ON DELETE CASCADE` и NOT NULL, а не
`SET NULL`. Причина — содержимое: `question` есть пересказ вопроса гостя, то есть
гостевой текст (строка в `docs/PII_REGISTRY.md` этим же PR), и переживать ретеншн
(spec 0032, 90 дней) он не вправе. Сам ретеншн сносит строку по её собственному
возрасту в `channels/common/retention.py` — каскад тут лишь второй рубеж.

Ни статуса, ни «скрыть»: окно страницы стирает список само (§7), а колонка
состояния потребовала бы действия менеджера ради того, что и так исчезнет.

Шаг безопасен и обратно-совместим целиком: одна новая таблица, ни одной правки
существующих. Старый образ в окне деплоя её просто не видит.
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
        "unanswered_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_unanswered_questions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_unanswered_questions_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unanswered_questions")),
    )
    op.create_index(
        op.f("ix_unanswered_questions_tenant_id"), "unanswered_questions", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_unanswered_questions_conversation_id"),
        "unanswered_questions",
        ["conversation_id"],
    )
    # Оба чтения таблицы фильтруют по возрасту: окно страницы 7 дней (§7) и
    # ретеншн 90 дней (spec 0032) — как 0026 фильтровала по created_at.
    op.create_index(
        op.f("ix_unanswered_questions_created_at"), "unanswered_questions", ["created_at"]
    )
    _apply_tenant_rls("unanswered_questions")


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table("unanswered_questions")
