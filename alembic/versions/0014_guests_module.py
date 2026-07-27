"""guests module: guests, guest_identities, stays, stay_access_codes, guest_sessions

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-27

Spec 0027 (issue #79), ADR-008 §3: гостевой мир — целиком тенантный, все пять
таблиц под RLS (канон 0002, `_apply_tenant_rls` скопирован оттуда целиком).

Первая PII-миграция платформы (§9): вместе с ней рождается
`docs/PII_REGISTRY.md` (guests.display_name, stays.room_number).

Секреты — только хэши (ADR-008): stay_access_codes.code_hash — bcrypt,
guest_sessions.token_hash — SHA-256; plaintext в БД не попадает никогда.

Partial unique индексы (не выразимы UniqueConstraint'ом, поэтому Index):
- один АКТИВНЫЙ Stay на комнату тенанта (WHERE status = 'checked_in');
- один активный код на Stay (WHERE revoked_at IS NULL).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _apply_tenant_rls(table_name: str) -> None:
    """КАНОН (скопирован из миграции 0002 — см. обоснование там)."""
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
        "guests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_guests_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guests")),
    )
    op.create_index(op.f("ix_guests_tenant_id"), "guests", ["tenant_id"])
    _apply_tenant_rls("guests")

    op.create_table(
        "guest_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_guest_identities_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["guest_id"],
            ["guests.id"],
            name=op.f("fk_guest_identities_guest_id_guests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guest_identities")),
    )
    op.create_index(op.f("ix_guest_identities_tenant_id"), "guest_identities", ["tenant_id"])
    op.create_index(op.f("ix_guest_identities_guest_id"), "guest_identities", ["guest_id"])
    op.create_index(
        "uq_guest_identities_tenant_kind_external",
        "guest_identities",
        ["tenant_id", "kind", "external_id"],
        unique=True,
    )
    _apply_tenant_rls("guest_identities")

    op.create_table(
        "stays",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("room_number", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("check_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_stays_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["guest_id"],
            ["guests.id"],
            name=op.f("fk_stays_guest_id_guests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stays")),
    )
    op.create_index(op.f("ix_stays_tenant_id"), "stays", ["tenant_id"])
    op.create_index(op.f("ix_stays_guest_id"), "stays", ["guest_id"])
    op.create_index(
        "uq_stays_tenant_room_checked_in",
        "stays",
        ["tenant_id", "room_number"],
        unique=True,
        postgresql_where=sa.text("status = 'checked_in'"),
    )
    _apply_tenant_rls("stays")

    op.create_table(
        "stay_access_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("stay_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_stay_access_codes_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stay_id"],
            ["stays.id"],
            name=op.f("fk_stay_access_codes_stay_id_stays"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stay_access_codes")),
    )
    op.create_index(op.f("ix_stay_access_codes_tenant_id"), "stay_access_codes", ["tenant_id"])
    op.create_index(op.f("ix_stay_access_codes_stay_id"), "stay_access_codes", ["stay_id"])
    op.create_index(
        "uq_stay_access_codes_active_stay",
        "stay_access_codes",
        ["stay_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    _apply_tenant_rls("stay_access_codes")

    op.create_table(
        "guest_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("stay_id", sa.Uuid(), nullable=False),
        sa.Column("guest_identity_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_version", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_guest_sessions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stay_id"],
            ["stays.id"],
            name=op.f("fk_guest_sessions_stay_id_stays"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["guest_identity_id"],
            ["guest_identities.id"],
            name=op.f("fk_guest_sessions_guest_identity_id_guest_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guest_sessions")),
    )
    op.create_index(op.f("ix_guest_sessions_tenant_id"), "guest_sessions", ["tenant_id"])
    op.create_index(op.f("ix_guest_sessions_stay_id"), "guest_sessions", ["stay_id"])
    op.create_index(
        op.f("ix_guest_sessions_guest_identity_id"), "guest_sessions", ["guest_identity_id"]
    )
    op.create_index(
        "uq_guest_sessions_tenant_token",
        "guest_sessions",
        ["tenant_id", "token_hash"],
        unique=True,
    )
    _apply_tenant_rls("guest_sessions")


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; порядок — обратный FK.
    op.drop_table("guest_sessions")
    op.drop_table("stay_access_codes")
    op.drop_table("stays")
    op.drop_table("guest_identities")
    op.drop_table("guests")
