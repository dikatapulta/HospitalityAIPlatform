"""requests: заявка знает своё проживание — service_requests.stay_id

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-30

Spec 0054 (issue #228): колонка `service_requests.stay_id` — Stay веб-гостя,
для которого заявка; квоты (#124) считают по нему, а не по строке комнаты.

- nullable — проживание известно только веб-гостю; Telegram, кабинет и HTTP API
  пишут NULL, старые строки не заполняются (бэкфилла нет, spec 0054 §4);
- БЕЗ FK на `stays`: таблица модуля guests, схема модуля requests на неё не
  ссылается (P-2) — как `room_service_orders.stay_id`;
- индекс — выборка заявок одного проживания.

RLS у таблицы уже есть (миграция 0006), колонка её не меняет. Шаг
обратно-совместим: старый образ колонку не читает и не пишет. Downgrade снимает
колонку вместе со связью заявок с проживаниями.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_REQUESTS = "service_requests"


def upgrade() -> None:
    op.add_column(_REQUESTS, sa.Column("stay_id", sa.Uuid(), nullable=True))
    op.create_index(op.f(f"ix_{_REQUESTS}_stay_id"), _REQUESTS, ["stay_id"])


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_REQUESTS}_stay_id"), table_name=_REQUESTS)
    op.drop_column(_REQUESTS, "stay_id")
