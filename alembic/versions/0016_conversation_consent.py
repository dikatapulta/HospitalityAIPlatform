"""conversations: consent_at/consent_version — факт согласия гостя на обработку ПД

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-29

Spec 0029 §1, issue #127, юраудит 22.07 (вариант A: «факт — conversations.
consent_at + consent_version»). Доказательная запись consent-gate'а канала
telegram: у телеграм-гостя нет ни Stay, ни сессии (auth-only в Telegram — не
эта задача), поэтому согласие живёт на диалоге. Веб хранит своё на
`guest_sessions` (миграция 0014) — там оно даётся на каждую привязку.

NULLABLE и без бэкфилла намеренно: у всех существующих диалогов согласия нет,
и это правда — они пройдут гейт при следующем сообщении гостя. Тип колонки
версии — VARCHAR(16), как `guest_sessions.consent_version` (формат «дата-vN»).
PII-реестр (docs/PII_REGISTRY.md) обновлён этим же PR — §9.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("consent_version", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "consent_version")
    op.drop_column("conversations", "consent_at")
