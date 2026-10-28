"""platform: службы на членстве и приглашении, уровень вместо роли ресепшена

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-24

Spec 0045 (issue #472). У членства две настройки — уровень (`role_key`:
staff | senior | manager) и службы (`service_keys`, ключи категорий заявок
отеля). Обе таблицы платформенные, вне RLS — whitelist-исключение P-4 то же,
что в миграции 0017; нового нет.

- `tenant_memberships.service_keys`, `staff_invites.service_keys` —
  `VARCHAR(63)[] NOT NULL DEFAULT '{}'`. Умолчание остаётся навсегда: старый
  образ при откате вставляет членства и приглашения без этой колонки.
- Данные: роль `receptionist` → уровень `staff` + служба `reception` (ресепшен
  теперь служба, spec 0045 §2), у членств и у приглашений — в том числе
  принятых и истёкших: строка с незнакомой ролью не читалась бы новым кодом.
  Прочие `staff` получают пустой список — службы им расставляет менеджер.

Старому образу схема не мешает, мешают ДАННЫЕ: уровень `senior` он не читает
(`LookupError` в SQLAlchemy `Enum`), а бывший ресепшен для него — `staff` без
заселения. `downgrade` возвращает данные к его виду; откат образа без
downgrade — тем же SQL (docs/runbooks/deploy.md, «Откат на образ старше #472»).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_TABLES = ("tenant_memberships", "staff_invites")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "service_keys",
                postgresql.ARRAY(sa.String(length=63)),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        op.execute(
            f"UPDATE {table} SET role_key = 'staff', service_keys = ARRAY['reception'] "
            "WHERE role_key = 'receptionist'"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"UPDATE {table} SET role_key = 'receptionist' "
            "WHERE role_key IN ('staff', 'senior') AND 'reception' = ANY(service_keys)"
        )
        op.execute(f"UPDATE {table} SET role_key = 'staff' WHERE role_key = 'senior'")
        op.drop_column(table, "service_keys")
