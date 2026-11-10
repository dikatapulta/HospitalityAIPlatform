"""requests: возврат выполненной в работу — service_requests.reopen_count

Revision ID: 0074
Revises: 0073
Create Date: 2026-11-11

Spec 0080 §2 (issue #55): выполненную заявку сотрудник может вернуть в работу в
течение суток после закрытия. Заявка помнит, сколько раз её возвращали:

- `reopen_count` — INTEGER NOT NULL, умолчание 0 и в БД: по нему уведомитель гостя
  различает закрытия одной заявки (ключ «выполнена» и поправки). Не ПД: число.

Таблица уже тенантная, RLS не меняется. Схема обратно-совместима: `server_default`
навсегда — старый образ вставляет строки без колонки и её не читает. Бэкфилла нет —
до этой миграции возвратов не было, 0 у всех верен. Downgrade удаляет колонку:
возвращённые заявки остаются как есть, счёт возвратов теряется.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

_REQUESTS = "service_requests"


def upgrade() -> None:
    op.add_column(
        _REQUESTS,
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column(_REQUESTS, "reopen_count")
