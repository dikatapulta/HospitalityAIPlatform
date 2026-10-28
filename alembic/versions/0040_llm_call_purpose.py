"""ai/gateway: назначение вызова — `purpose` у журнала и резервов бюджета

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-28

ADR-024 §1 (issue #492, PR 2 серии spec 0048): у гостевого бота и «Наставника»
персонала своя модель и свой дневной бюджет. Расход за сутки и живые резервы
считаются в пределах своего назначения, поэтому назначение пишется в обе
таблицы бюджета.

- `purpose VARCHAR(16) NOT NULL DEFAULT 'guest'` — без CHECK, как `status`:
  новое назначение миграции не требует. Существующие строки получают `'guest'`
  умолчанием самого ADD COLUMN — все они от гостевого пути, бэкфилла нет.
- `server_default` остаётся насовсем: старый образ в окне деплоя (и при
  откате) вставляет строки без `purpose`, и умолчание делает их гостевыми.
  Снять его — сломать откат.

Индекса нет: обе выборки бюджета уже идут по индексу `created_at` /
`reserved_until` за одни сутки одного тенанта.

Откат снимает колонки: строки наставника (с PR 3 серии) старый код посчитает
в гостевой бюджет — не больше `LLM_MENTOR_DAILY_BUDGET_USD` в сутки сверху.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_TABLES = ("llm_call_log", "llm_budget_reservation")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("purpose", sa.String(length=16), nullable=False, server_default="guest"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "purpose")
