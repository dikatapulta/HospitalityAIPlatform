"""requests: службы создателя заявки формой — service_requests.created_by_service_keys

Revision ID: 0056
Revises: 0055
Create Date: 2026-11-03

Spec 0063 §2 (issue #700): снимок служб сотрудника, создавшего заявку формой
кабинета, на момент создания. По нему вкладка «Отправленные» показывает службе
заявки, которые она отправила другим. Ключи служб — не ПД (имени и id автора
нет), docs/PII_REGISTRY.md не меняется. Таблица уже тенантная, RLS не меняется.

`server_default` остаётся навсегда: старый образ вставляет строки без колонки
(откат образа). Бэкфилла нет — снимок «из нынешнего членства» был бы выдумкой.
Downgrade удаляет колонку — вкладка опустеет, заявки не меняются.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column(
            "created_by_service_keys",
            postgresql.ARRAY(sa.String(length=63)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("service_requests", "created_by_service_keys")
