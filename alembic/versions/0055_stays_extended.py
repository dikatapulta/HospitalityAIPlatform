"""guests: продление для хаускипинга — stays.extended_at / extended_from

Revision ID: 0055
Revises: 0054
Create Date: 2026-11-02

Spec 0062 §3 (issue #668): когда последний раз вырос срок выезда Stay и каким
он был до этого. Пишут кнопка «+N ночей» кабинета и сверка Opera — только когда
новый срок позже прежнего. Доска выездов по ним показывает «продлено до 07.11»
и зовёт горничную этажа. Это даты проживания, как `check_out_at`: новых ПД нет
(docs/PII_REGISTRY.md не меняется). Таблица уже тенантная, RLS не меняется.

Шаг обратно-совместим: старый образ колонки не читает, у старых строк — NULL.
Downgrade удаляет колонки — доска перестанет знать о продлениях, сроки Stay
не меняются.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stays", sa.Column("extended_at", sa.DateTime(timezone=True)))
    op.add_column("stays", sa.Column("extended_from", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("stays", "extended_from")
    op.drop_column("stays", "extended_at")
