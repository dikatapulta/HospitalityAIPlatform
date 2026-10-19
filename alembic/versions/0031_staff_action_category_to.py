"""platform: staff_action_log.category_name_to — куда передали заявку

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-17

Spec 0042 (issue #216): журнал изменений пишет передачу заявки в другую службу
строкой «#12 · Уборка → Инженер · 305». `category_name` — откуда (как и у
остальных действий с заявкой), новая колонка — куда, по образцу пары
`room_number`/`room_number_to` у переселения. Снапшот подписи службы, не FK:
журнал свидетельствует «как было в момент действия» (spec 0041 §2.1).

Шаг обратно-совместим целиком: одна nullable-колонка без умолчания. Старый
образ в окне деплоя её не видит и не пишет. Downgrade удаляет колонку вместе
со значениями «куда» — строки журнала остаются, теряется только вторая служба
у передач.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff_action_log",
        sa.Column("category_name_to", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("staff_action_log", "category_name_to")
