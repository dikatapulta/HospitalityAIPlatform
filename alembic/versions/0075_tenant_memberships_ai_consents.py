"""tenant_memberships.ai_consents — отметка согласия сотрудника на ИИ-разделы

Revision ID: 0075
Revises: 0073
Create Date: 2026-11-11

Spec 0082, issue #985: «Наставник» отправляет тексты сотрудника в OpenAI, «Предложить
идею» — в Anthropic (США), и основание — письменное согласие работника на каждого
получателя. Менеджер отмечает подписанное согласие в «Сотрудниках»; без отметки раздел
сотруднику закрыт.

- `ai_consents VARCHAR(16)[] NOT NULL DEFAULT '{}'` — значения `AiRecipient`
  (`openai`, `anthropic`). Таблица — whitelist-исключение P-4 без RLS (миграция 0017).
- Бэкфилла нет: умолчание «согласия нет», и выкат никого не включает — на копии отеля
  «Наставник» живым сотрудникам ещё не открыт.

Шаг обратно-совместим: старый образ колонку не читает и вставляет членства без неё —
поэтому умолчание остаётся навсегда (довод миграции 0033). Downgrade удаляет колонку —
отметки теряются, разделы снова открыты всем уровням, как до этой миграции.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0075"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_memberships",
        sa.Column(
            "ai_consents",
            postgresql.ARRAY(sa.String(length=16)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_memberships", "ai_consents")
