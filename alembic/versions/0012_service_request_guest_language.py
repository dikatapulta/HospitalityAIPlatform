"""service_requests: guest_language — язык гостя для статусных уведомлений

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-18

Spec 0021, Проблема 1 (issue #77): системные сообщения гостю о заявке
(выполнена/отменена) должны звучать на языке, на котором гость её просил.
Язык — снимок на момент создания заявки: ISO 639-1 код из аргумента
инструмента `create_service_request.guest_language`.

NULLABLE намеренно: заявки, созданные до миграции или мимо инструмента
(HTTP API без поля), языка не имеют — уведомление уходит на
`default_language` конфига тенанта (issue #66), при отсутствии конфига —
по-русски (платформенный дефолт).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests", sa.Column("guest_language", sa.String(length=2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("service_requests", "guest_language")
