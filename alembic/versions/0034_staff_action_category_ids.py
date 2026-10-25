"""platform: staff_action_log.category_id / category_id_to — служба строки журнала

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-25

Spec 0045 §3 (PR 3, issue #472): старший службы видит в журнале изменений
строки, где служба «откуда» или «куда» — одна из его служб. Журнал хранил
службу только подписью (`category_name`), а подпись не уникальна и меняется
свободно — по ней границу не провести. Новые колонки — id категории, как
`request_id`: без FK, ядро не ссылается на таблицы модулей. У передачи
`category_id` — откуда, `category_id_to` — куда (пара `category_name_to`, 0031).

Старые строки не заполняются: их видят менеджер и ресепшен, как и до этой
миграции, а старшему они не показываются — граница не открывает того, в чём
не уверена. На копии пилота заявки выключены (#384), строк заявок там почти нет.

Шаг обратно-совместим целиком: две nullable-колонки без умолчания. Старый
образ их не видит и не пишет. Downgrade удаляет колонки — строки журнала
остаются, теряется только служба для старшего.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("staff_action_log", sa.Column("category_id", sa.Uuid(), nullable=True))
    op.add_column("staff_action_log", sa.Column("category_id_to", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("staff_action_log", "category_id_to")
    op.drop_column("staff_action_log", "category_id")
