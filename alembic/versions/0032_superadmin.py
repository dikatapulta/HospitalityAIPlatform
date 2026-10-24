"""platform: суперадмин — второй фактор и ссылки нового пароля

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-24

Spec 0044 (issue #377). Две ПЛАТФОРМЕННЫЕ таблицы, обе без `tenant_id` и без
RLS, как `users` (ADR-008 §6): нового whitelist-исключения P-4 нет.

- `staff_second_factors` — секрет кода из приложения суперадмина и шаг
  последнего принятого кода (защита от повтора). Секрет открытым — spec 0044 §7.
- `staff_password_resets` — одноразовые ссылки нового пароля: идентичность,
  SHA-256 токена, кто выдал, срок, когда использована.

Новый статус `users.status = 'erased'` схемы не меняет: колонка — VARCHAR без
CHECK (`_user_status_column_type`).

Шаг обратно-совместим по схеме: старый образ новых таблиц не видит. Мешают ему
ДАННЫЕ (рецепт — docs/runbooks/deploy.md, «Откат на образ старше #377»): строку
`users` со статусом `erased` он не читает (`LookupError` в SQLAlchemy `Enum`), а
суперадмина пустил бы по одному паролю — второго фактора он не знает.
`downgrade` поэтому приводит данные к виду, безопасному для старого кода:
стёртые → `deactivated` (вход им и так закрыт — хэш пароля стёрт), у
суперадмина стирается хэш пароля — после возврата на новый образ пароль
задаётся командой `superadmin set-password`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_second_factors",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("totp_secret", sa.String(length=64), nullable=False),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_staff_second_factors_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_staff_second_factors")),
    )

    op.create_table(
        "staff_password_resets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_by", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id"],
            ["user_identities.id"],
            name=op.f("fk_staff_password_resets_identity_id_user_identities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"],
            ["users.id"],
            name=op.f("fk_staff_password_resets_issued_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_password_resets")),
    )
    op.create_index(
        op.f("ix_staff_password_resets_identity_id"), "staff_password_resets", ["identity_id"]
    )
    op.create_index(
        "uq_staff_password_resets_token_hash",
        "staff_password_resets",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.execute("UPDATE users SET status = 'deactivated' WHERE status = 'erased'")
    op.execute(
        "UPDATE user_identities SET secret_hash = NULL "
        "WHERE user_id IN (SELECT id FROM users WHERE is_platform_admin)"
    )
    op.drop_index("uq_staff_password_resets_token_hash", table_name="staff_password_resets")
    op.drop_index(op.f("ix_staff_password_resets_identity_id"), table_name="staff_password_resets")
    op.drop_table("staff_password_resets")
    op.drop_table("staff_second_factors")
