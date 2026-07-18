"""service_requests: resolution_note — примечание персонала к закрытию заявки

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-18

Spec 0021, Проблема 4 (issue #76): заявка может быть выполнена не полностью
(«полотенца принесли, кофе закончился») или отменена по причине — персонал
пишет короткое примечание по-русски, система доносит его до гостя на языке
гостя частью уведомления о закрытии. Отдельный статус «done_partial» не
вводится: частичность — свойство завершения, а не стадия жизненного цикла.

NULLABLE: примечание опционально и осмысленно только у терминальных статусов
(пишет его `change_request_status` и только на переходе в done/cancelled).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests", sa.Column("resolution_note", sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("service_requests", "resolution_note")
