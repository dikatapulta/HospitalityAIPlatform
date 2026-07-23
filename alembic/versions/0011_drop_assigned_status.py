"""Жизненный цикл заявки без assigned: данные assigned → in_progress

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-18

ADR-013 (issue #75): статус `assigned` удаляется из жизненного цикла —
персонал пилота не различал «назначено» и «в работе», состояние не несло
смысла. Колонка `status` — VARCHAR (`native_enum=False`, решение Task 0012
ровно на такой случай), поэтому изменение состава значений — обычная миграция
данных без ALTER TYPE.

Downgrade намеренно не восстанавливает `assigned`: различие «назначено, но не
начато» после апгрейда утеряно необратимо (ADR-013, «Последствия»). Схема БД
не меняется, поэтому downgrade — no-op.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE service_requests SET status = 'in_progress' WHERE status = 'assigned'")


def downgrade() -> None:
    pass
