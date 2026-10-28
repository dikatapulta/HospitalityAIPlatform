"""platform: staff_push_subscriptions — подписки телефонов персонала на веб-пуш

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-28

Spec 0050 (issue #477): телефон сотрудника подписывается на уведомления кабинета,
сервер шлёт пуш по адресу подписки. Подписка — на членстве (тенант + сотрудник),
а не на сессии: idle-срок сессии не должен глушить телефон в кармане. Стирание
учётки удаляет подписки каскадом (FK на `users`).

UNIQUE `(tenant_id, endpoint)` — один телефон в отеле принадлежит одному
сотруднику: вход под другим логином переносит подписку upsert'ом.

Шаг обратно-совместим целиком: одна новая таблица. Старый образ в окне деплоя
её не видит. Downgrade удаляет таблицу с подписками — телефоны переподпишутся
сами при открытии кабинета на новом образе.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_TABLE = "staff_push_subscriptions"


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
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=1024), nullable=False),
        sa.Column("p256dh", sa.String(length=100), nullable=False),
        sa.Column("auth", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"], unique=False)
    op.create_index(
        "uq_staff_push_subscriptions_tenant_endpoint",
        _TABLE,
        ["tenant_id", "endpoint"],
        unique=True,
    )
    op.create_index(
        "ix_staff_push_subscriptions_tenant_user",
        _TABLE,
        ["tenant_id", "user_id"],
        unique=False,
    )
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    op.drop_table(_TABLE)
