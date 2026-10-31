"""requests: причина отмены заявки — cancel_reason

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-31

Spec 0057 (issue #54): причина отмены — значение из списка, а не свободный
текст; по ней выбирается текст гостю и считается «почему отменяем».

- колонка nullable: у отменённых до миграции причины нет, бэкфилла нет —
  свободный текст `resolution_note` в значение списка надёжно не переводится;
- VARCHAR(16) со значениями `CancelReason.value`, без CHECK состава — канон
  `origin` (миграция 0025): состав меняется миграцией данных;
- CHECK: причина только у `cancelled` — терминального статуса, из которого
  переходов нет.

RLS у таблицы уже есть (миграция 0006), колонка её не меняет. Шаг
обратно-совместим: старый образ колонку не читает и не пишет. Откат снимает
колонку вместе с причинами — тексты гостю уже ушли, теряется только счёт.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_REQUESTS = "service_requests"
_CHECK = f"ck_{_REQUESTS}_cancel_reason_only_cancelled"


def upgrade() -> None:
    op.add_column(_REQUESTS, sa.Column("cancel_reason", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f(_CHECK), _REQUESTS, "cancel_reason IS NULL OR status = 'cancelled'"
    )


def downgrade() -> None:
    op.drop_constraint(op.f(_CHECK), _REQUESTS, type_="check")
    op.drop_column(_REQUESTS, "cancel_reason")
