"""requests: гость напоминает о заявке — service_requests.guest_reminder_count/_reminded_at

Revision ID: 0061
Revises: 0060
Create Date: 2026-11-04

Spec 0070 §3 (issue #834, PR 1): сколько раз гость напомнил о заявке и когда
последний раз. По ним лестница кабинета поднимает заявку к ресепшену и
менеджеру (spec 0070 §5). Число и время — не текст гостя: новых ПД нет,
docs/PII_REGISTRY.md не меняется. Таблица уже тенантная, RLS не меняется.

`server_default` счёта остаётся навсегда: старый образ вставляет строки без
колонки (откат образа). Бэкфилла нет — прошлых напоминаний никто не считал.
Downgrade удаляет обе колонки — счёт напоминаний теряется, заявки не меняются.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column(
            "guest_reminder_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "service_requests",
        sa.Column("guest_reminded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_service_requests_guest_reminder_count_non_negative"),
        "service_requests",
        "guest_reminder_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_service_requests_guest_reminder_count_non_negative"),
        "service_requests",
        type_="check",
    )
    op.drop_column("service_requests", "guest_reminded_at")
    op.drop_column("service_requests", "guest_reminder_count")
