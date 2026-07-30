"""service_requests: claimed_by_user_id + claimed_by_display_name — кто взял заявку

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-30

Spec 0033 §5 (issue #48, PR D серии): кнопка «Взять» в кабинете персонала
пишет, КТО взял заявку в работу. Два поля:

- `claimed_by_user_id` — FK на платформенных `users` (по образцу FK на
  `tenants`: тенантная таблица ссылается на платформенный мир). ondelete
  SET NULL, а не CASCADE: User в v1 не удаляется (только деактивация), но
  даже гипотетическое удаление не должно уносить заявки отеля.
- `claimed_by_display_name` — денормализованный снапшот имени на момент
  взятия: список заявок не ходит в платформенные таблицы, а имя на момент
  взятия — честная история (сотрудника потом могли переименовать или
  деактивировать). PII — строка в docs/PII_REGISTRY.md в этом же PR.

Оба NULLABLE: Telegram-путь персонала идентичности не несёт (staff-чат —
канальный суррогат, ADR-008 §7) и оставляет колонки пустыми; заполняет их
только переход new → in_progress с `acting_user` из кабинета.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_requests", sa.Column("claimed_by_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "service_requests",
        sa.Column("claimed_by_display_name", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_service_requests_claimed_by_user_id_users"),
        "service_requests",
        "users",
        ["claimed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_service_requests_claimed_by_user_id_users"),
        "service_requests",
        type_="foreignkey",
    )
    op.drop_column("service_requests", "claimed_by_display_name")
    op.drop_column("service_requests", "claimed_by_user_id")
