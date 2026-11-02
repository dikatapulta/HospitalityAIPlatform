"""housekeeping: «срочно к часу» — room_turnovers.needed_by_at

Revision ID: 0054
Revises: 0053
Create Date: 2026-11-02

Spec 0062 §2 (issue #668): ресепшен или старшая отмечает, к какому часу номер
нужен (ранний заезд), и путь поднимается на доске группой «Срочно».
`needed_by_at` — момент «номер нужен к» (UTC), NULL — метки нет. Кто поставил,
не хранится — новых ПД работников нет (docs/PII_REGISTRY.md не меняется).
Таблица уже тенантная, RLS не меняется.

Шаг обратно-совместим: старый образ колонку не читает, у старых строк — NULL.
Downgrade удаляет колонку вместе с метками открытых путей — доска вернётся к
общей очереди.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("room_turnovers", sa.Column("needed_by_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("room_turnovers", "needed_by_at")
