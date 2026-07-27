"""conversations: guest_identity_id — привязка диалога к идентичности гостя

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-27

Spec 0027 §3.2, ADR-008 §3 («Conversation канала получает ссылку на
GuestIdentity аддитивно»): веб-диалог рождается привязанным к идентичности,
созданной при вводе кода заселения. NULLABLE и БЕЗ FK — граница модулей
(таблица канала не связывает свою схему с таблицей модуля guests; тот же
довод, что у request_origins.request_id, ADR-011): telegram-диалоги остаются
NULL до включения auth-only в Telegram.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("guest_identity_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "guest_identity_id")
