"""stays: guests_count — число гостей проживания

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-30

Spec 0033 §6 (issue #48, PR E серии): страница заселения кабинета спрашивает
число гостей кнопками «1/2/3+» («3+» хранится как 3). Колонка — вход будущего
конфига квот #124 («лимит × guests_count»): субъект квоты — Stay × guests_count,
без числа гостей «вода на каждого гостя» неисполнима.

server_default "1" остаётся насовсем: существующие Stay (CLI до кабинета)
считаются одноместными, а само значение 1 — честный дефолт и для будущих
вставок мимо ORM. Не PII — строки в docs/PII_REGISTRY.md не требует.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stays",
        sa.Column("guests_count", sa.SmallInteger(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("stays", "guests_count")
