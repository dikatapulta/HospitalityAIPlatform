"""requests: кому направлена заявка — service_requests.directed_to_user_id и _display_name

Revision ID: 0062
Revises: 0061
Create Date: 2026-11-05

Spec 0053 §4а (issue #856): старший уборки или менеджер «направляет» невзятую
заявку горничной другого этажа — заявка видна и звучит ей. Пара полей по
образцу `claimed_by_*` (миграция 0018):

- `directed_to_user_id` — FK на платформенных `users`, ondelete SET NULL: удаление
  учётки не уносит заявки отеля;
- `directed_to_display_name` — снапшот имени на момент направления: лента не
  ходит в платформенные таблицы. PII сотрудника — строка в docs/PII_REGISTRY.md
  в этом же PR.

Таблица уже тенантная, RLS не меняется. Шаг обратно-совместим: обе колонки
NULLABLE без умолчания, старый образ их не пишет и не читает. Бэкфилла нет — до
этой миграции заявки никому не направляли. Downgrade удаляет колонки: срез по
этажам перестанет пропускать направленные их адресату, заявки не меняются.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_requests", sa.Column("directed_to_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "service_requests",
        sa.Column("directed_to_display_name", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_service_requests_directed_to_user_id_users"),
        "service_requests",
        "users",
        ["directed_to_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_service_requests_directed_to_user_id_users"),
        "service_requests",
        type_="foreignkey",
    )
    op.drop_column("service_requests", "directed_to_display_name")
    op.drop_column("service_requests", "directed_to_user_id")
