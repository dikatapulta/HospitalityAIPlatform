"""staff_sessions.remembered — «Запомнить меня» на входе кабинета

Revision ID: 0066
Revises: 0065
Create Date: 2026-11-10

Spec 0073 §3 (issue #378): сессия без галочки «Запомнить меня» запирается после
`staff_session_short_idle_minutes` без действий человека — на общем компьютере
стойки следующий не сработает под чужим именем.

- `remembered boolean NOT NULL DEFAULT false`. Таблица платформенная, вне RLS
  (ADR-008 §6). Умолчание БД остаётся навсегда: путь создания сессии, забывший
  про флаг, получит короткую — безопасную — сессию. Сессии, открытые до выката,
  этим же умолчанием становятся короткими (spec 0073 §2): забытые на стойке
  запираются сразу.

Шаг обратно-совместим: старый образ колонку не пишет (умолчание) и не читает.
Downgrade удаляет колонку — все сессии снова живут прежние 30 дней простоя.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff_sessions",
        sa.Column("remembered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("staff_sessions", "remembered")
