"""requests: исходная заявка остатка — service_requests.parent_request_id

Revision ID: 0073
Revises: 0072
Create Date: 2026-11-10

Spec 0079 §3 (issue #58): сотрудник закрывает заявку «Готово» с примечанием и
оставляет остальное новой заявкой в очереди службы. Остаток помнит исходную:

- `parent_request_id` — FK на `service_requests.id` той же таблицы, ondelete
  SET NULL: удаление исходной не уносит остаток. Индекс — по нему ищут остатки
  заявки. Не ПД: ссылка на заявку.

Таблица уже тенантная, RLS не меняется. Схема обратно-совместима: колонка
NULLABLE без умолчания, старый образ её не пишет и не читает. Бэкфилла нет — до
этой миграции остатков не было. Мешают старому образу только данные — заявки
`origin = 'remainder'` (рунбук deploy.md, «Откат на образ старше #58»).
Downgrade удаляет колонку: остатки остаются заявками, связь с исходной теряется.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

_REQUESTS = "service_requests"


def upgrade() -> None:
    op.add_column(_REQUESTS, sa.Column("parent_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{_REQUESTS}_parent_request_id_{_REQUESTS}"),
        _REQUESTS,
        _REQUESTS,
        ["parent_request_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f(f"ix_{_REQUESTS}_parent_request_id"), _REQUESTS, ["parent_request_id"])


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_REQUESTS}_parent_request_id"), table_name=_REQUESTS)
    op.drop_constraint(
        op.f(f"fk_{_REQUESTS}_parent_request_id_{_REQUESTS}"), _REQUESTS, type_="foreignkey"
    )
    op.drop_column(_REQUESTS, "parent_request_id")
