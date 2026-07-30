"""staff identity: users, user_identities, tenant_memberships, staff_sessions, staff_invites

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-30

Spec 0033 §3.1 (issue #48), ADR-008 §1/§6: staff-половина модели идентичности.
Все пять таблиц — ПЛАТФОРМЕННЫЙ мир, сознательно БЕЗ RLS (как `tenants`):
это механизм, который устанавливает контекст тенанта, — он работает до того,
как контекст есть (login, проверка сессии), и поверх тенантов (membership
many-to-many, platform_admin). Правило ADR-003 «каждая тенантная таблица —
под RLS» не нарушается: таблицы не тенантные. Компенсация — доступ только
из кода `platform/` (граница R-5).

WHITELIST-ИСКЛЮЧЕНИЯ ИЗ P-4 (ADR-008 §6): таблицы с колонкой `tenant_id`
БЕЗ RLS-политики. Будущий автотест/линтер «таблица с tenant_id обязана иметь
RLS» обязан знать этот список явно, а не выводить его:
- `tenant_memberships` — членство проверяется до установки контекста тенанта;
- `staff_invites` — инвайт принимает человек без сессии и без контекста.
(Третьим в списке станет `service_accounts` — follow-up ADR-008 §2.)

PII-миграция (§9): в том же PR — строки в docs/PII_REGISTRY.md
(users.display_name, user_identities.external_id для kind=password — email,
staff_invites.invited_name).

Секреты — только хэши (ADR-008): user_identities.secret_hash — argon2id,
staff_sessions.token_hash и staff_invites.token_hash — SHA-256; plaintext
в БД не попадает никогда.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )

    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("secret_hash", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_identities")),
    )
    op.create_index(op.f("ix_user_identities_user_id"), "user_identities", ["user_id"])
    op.create_index(
        "uq_user_identities_kind_external",
        "user_identities",
        ["kind", "external_id"],
        unique=True,
    )

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("role_key", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tenant_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_memberships_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f("fk_tenant_memberships_invited_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_memberships")),
    )
    op.create_index(op.f("ix_tenant_memberships_user_id"), "tenant_memberships", ["user_id"])
    op.create_index(op.f("ix_tenant_memberships_tenant_id"), "tenant_memberships", ["tenant_id"])
    op.create_index(
        "uq_tenant_memberships_user_tenant",
        "tenant_memberships",
        ["user_id", "tenant_id"],
        unique=True,
    )

    op.create_table(
        "staff_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_staff_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_sessions")),
    )
    op.create_index(op.f("ix_staff_sessions_user_id"), "staff_sessions", ["user_id"])
    op.create_index("uq_staff_sessions_token_hash", "staff_sessions", ["token_hash"], unique=True)

    op.create_table(
        "staff_invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("role_key", sa.String(length=32), nullable=False),
        sa.Column("invited_name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_staff_invites_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f("fk_staff_invites_invited_by_users"),
        ),
        sa.ForeignKeyConstraint(
            ["accepted_user_id"],
            ["users.id"],
            name=op.f("fk_staff_invites_accepted_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_invites")),
    )
    op.create_index(op.f("ix_staff_invites_tenant_id"), "staff_invites", ["tenant_id"])
    op.create_index(op.f("ix_staff_invites_invited_by"), "staff_invites", ["invited_by"])
    op.create_index("uq_staff_invites_token_hash", "staff_invites", ["token_hash"], unique=True)


def downgrade() -> None:
    # Порядок — обратный FK: сначала таблицы, ссылающиеся на users/tenants.
    op.drop_table("staff_invites")
    op.drop_table("staff_sessions")
    op.drop_table("tenant_memberships")
    op.drop_table("user_identities")
    op.drop_table("users")
