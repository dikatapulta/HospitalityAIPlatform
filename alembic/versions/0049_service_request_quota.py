"""requests: заявка по квоте бесплатного — quota_key, quota_units

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-30

Spec 0056 §3 (issue #124, PR 1 серии): заявка помнит, по какой квоте отеля
она бесплатна и сколько штук; сумма штук за день отеля по проживанию —
«уже заказано» (`requests.api.check_quota`).

- обе колонки nullable: заявка без квоты (почти все) их не несёт; бэкфилла
  нет — до этой миграции квот не было;
- CHECK пары: ключ без штук счёт не сложит, штуки без ключа ни к чему не
  относятся; CHECK 1–20 — тот же предел, что у нормы в конфиге
  (`platform.service_quotas.MAX_QUOTA_UNITS`);
- индекса нет: счёт идёт по индексу `stay_id` (миграция 0048) — у одного
  проживания за день единицы заявок.

RLS у таблицы уже есть (миграция 0006), колонки её не меняют. Шаг
обратно-совместим: старый образ колонки не читает и не пишет. Откат снимает
колонки вместе с метками квот — счёт на сегодня обнулится, и гостю один день
выдадут сверх нормы.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_REQUESTS = "service_requests"


def upgrade() -> None:
    op.add_column(_REQUESTS, sa.Column("quota_key", sa.String(length=63), nullable=True))
    op.add_column(_REQUESTS, sa.Column("quota_units", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{_REQUESTS}_quota_pair"),
        _REQUESTS,
        "(quota_key IS NULL) = (quota_units IS NULL)",
    )
    op.create_check_constraint(
        op.f(f"ck_{_REQUESTS}_quota_units_range"),
        _REQUESTS,
        "quota_units BETWEEN 1 AND 20",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(f"ck_{_REQUESTS}_quota_units_range"), _REQUESTS, type_="check")
    op.drop_constraint(op.f(f"ck_{_REQUESTS}_quota_pair"), _REQUESTS, type_="check")
    op.drop_column(_REQUESTS, "quota_units")
    op.drop_column(_REQUESTS, "quota_key")
