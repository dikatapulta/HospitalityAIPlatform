"""service_requests: is_urgent — признак срочности заявки

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-19

Spec 0034 §5 (issue #208, находка А-3 операционного аудита 05.08.2026):
у заявки не было признака срочности, поэтому «в номере течёт» стояло в очереди
наравне с «принесите полотенце», а гейт подтверждения P-9 требовал от гостя
отдельного «да» даже на аварию.

Колонка NOT NULL с `server_default false`: «неизвестно, срочная ли» — не
состояние (заявка либо срочная, либо обычная), а существующие заявки срочными
не были. `server_default` остаётся насовсем — он же честное умолчание для
вставок мимо ORM. Не PII: булев признак не описывает гостя, строки в
docs/PII_REGISTRY.md не требует.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column("is_urgent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("service_requests", "is_urgent")
