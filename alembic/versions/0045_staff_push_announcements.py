"""platform: staff_push_announcements и primed_at — рассылка пуша персоналу

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-29

Spec 0050 §6 (issue #477, PR 2): воркер раз в 15 с зовёт телефоны о том же, о чём
вкладка кабинета (`signal.build_signal`). Журнал `staff_push_announcements`
хранит, о чём сотруднику уже сообщили, — серверная копия `sessionStorage`
вкладки (`signal.js` `compare()`). После прохода в нём только ключи того, что
зовёт сейчас: взятая заявка выпадает сама, ретеншн не нужен. Ключ — строка
вида `new:<id заявки>:<id службы>`, `overdue:<id>`, `raised:<id>`,
`call:<id номера доски>:<шаг>`; текста гостя в ней нет.

`staff_push_subscriptions.primed_at` — подписка прошла первое включение:
до этого рассылка вносит в журнал текущие несрочные заявки молча, звучат только
срочные (spec 0039 §3.3).

Шаг обратно-совместим целиком: новая таблица и nullable-колонка. Старый образ в
окне деплоя их не видит. Downgrade удаляет журнал и колонку — после отката и
повторного обновления сотрудники один раз получат уведомления о том, что уже
ждёт (новый журнал пуст, подписки снова «первые» — срочные звучат, остальное
молча).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_TABLE = "staff_push_announcements"
_SUBSCRIPTIONS = "staff_push_subscriptions"


def _apply_tenant_rls(table_name: str) -> None:
    """КАНОН (копия из миграции 0002 — см. обоснование в её докстринге)."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table_name}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("call_key", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{_TABLE}_user_id_users"),
            ondelete="CASCADE",
        ),
        # Ключ начинается с tenant_id — отдельный индекс по нему не нужен.
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "call_key", name=op.f(f"pk_{_TABLE}")),
    )
    _apply_tenant_rls(_TABLE)
    op.add_column(_SUBSCRIPTIONS, sa.Column("primed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(_SUBSCRIPTIONS, "primed_at")
    op.drop_table(_TABLE)
