"""outbox: колонка next_attempt_at для backoff доставки

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11

Backoff между попытками доставки одного события (issue #18, ADR-009):
после неудачи `_deliver_one` откладывает следующую попытку, записывая
`next_attempt_at`; диспетчер (`deliver_pending_events`) не берёт строку в
работу, пока `next_attempt_at` не наступил. `NULL` — событие ещё не пыталось
доставляться (или уже доставлено) и берётся в работу немедленно, как раньше.

Отдельный partial-индекс не заводим (как и для `processed_at` в 0003) —
объём Фазы 0 не требует; фильтр по `processed_at IS NULL` уже использует
существующий индекс.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "next_attempt_at")
