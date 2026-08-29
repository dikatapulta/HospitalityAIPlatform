"""channels/common: conversation_escalations — durable-счёт эскалаций

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-29

Spec 0035 §6.1 (issue #300): число «бот звал сотрудника N раз за день» брать
было неоткуда — эскалация жила событием `conversation.escalated` в outbox и
строкой в логе. Считать `outbox_events` значит сделать механизм доставки
хранилищем событий вопреки ADR-005 (срок жизни строки там — настройка
ретеншна, а не свойство факта); считать staff-уведомления в `messages` —
скрытая связь и молчаливое занижение (при ненастроенном staff-чате уведомления
нет, а эскалация была). Поэтому своя тенантная таблица.

`conversation_id` — `ON DELETE SET NULL` и NULLABLE: ретеншн гостевых текстов
(spec 0032) через 90 дней сносит старые диалоги, а факт «бот позвал человека»
текста гостя не содержит и переживать ретеншн вправе.

Шаг безопасен и обратно-совместим целиком: одна новая таблица, ни одной правки
существующих. Старый образ в окне деплоя её просто не видит — в отличие от
соседней 0025, которая сняла `server_default` (её рецепт отката — runbook
деплоя, часть C; сюда он не относится).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
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
        "conversation_escalations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_conversation_escalations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_escalations_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_escalations")),
    )
    op.create_index(
        op.f("ix_conversation_escalations_tenant_id"), "conversation_escalations", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_conversation_escalations_conversation_id"),
        "conversation_escalations",
        ["conversation_id"],
    )
    # Единственное чтение таблицы — счёт за сутки отеля (`count_escalations`):
    # оно фильтрует по created_at, как 0023 фильтровала по reserved_until.
    op.create_index(
        op.f("ix_conversation_escalations_created_at"), "conversation_escalations", ["created_at"]
    )
    _apply_tenant_rls("conversation_escalations")


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table("conversation_escalations")
