"""staff_invites.revoked_at — отозванное приглашение отличимо от истёкшего

Revision ID: 0072
Revises: 0070
Create Date: 2026-11-10

Spec 0037 §4, issue #944: истёкшее непринятое приглашение остаётся в списке
«Сотрудников» с пометкой «истекла» и кнопкой «Новая ссылка», а отозванное
уходит. До этой миграции отзыв писал только `expires_at = now`, и два исхода
были неразличимы.

- `revoked_at timestamptz NULL`. Таблица — whitelist-исключение P-4 без RLS
  (как `tenant_memberships`), поэтому бэкфилл — обычный UPDATE.
- Бэкфилл: все непринятые приглашения, уже мёртвые к выкату, — отозванными
  (`revoked_at = expires_at`). Отличить в них отозванное от истёкшего нечем,
  а показать менеджеру отозванное «истёкшим» — воскресить то, что он убрал;
  истёкшие до выката из списка ушли и раньше.

Шаг обратно-совместим: старый образ колонку не читает, а новый отзыв пишет и
`expires_at = now`, как раньше. Downgrade удаляет колонку — истёкшие
приглашения снова пропадают из списка.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff_invites",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE staff_invites SET revoked_at = expires_at "
        "WHERE accepted_at IS NULL AND expires_at <= now()"
    )


def downgrade() -> None:
    op.drop_column("staff_invites", "revoked_at")
