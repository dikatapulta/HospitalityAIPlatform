"""documents: act_requests и act_request_files — запросы реквизитов для АВР

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-28

Spec 0049 §3 (issue #493, PR 2 серии): гость присылает реквизиты компании
формой — строка запроса живёт циклом своей заявки в службе «Бухгалтерия»
(`request_id` без FK, spec 0038 Р-3), файлы — строки `uploaded_files` узла.
Обе таблицы тенантные по канону 0002: RLS ENABLE + FORCE + политика
`tenant_isolation`; права роли приложения — через ALTER DEFAULT PRIVILEGES
миграции 0002.

Реквизиты и текст гостя — только шифртекстом (`*_ciphertext`, BYTEA,
FOUNDATION §11, Р-3). `edited_fields` — поля, которые правил человек: повторный
разбор их не трогает (Р-4). `request_created` — заявка точно создана: false
старше суток — брошенный запрос, его снимает ретеншн.

Шаг обратно-совместим целиком: две новые таблицы, старые не тронуты, старый
образ их не читает. Downgrade удаляет обе вместе с запросами и связями; файлы
узла остаются строками `uploaded_files` и уходят ретеншном по своему сроку.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_REQUESTS = "act_requests"
_FILES = "act_request_files"


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
        _REQUESTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_created", sa.Boolean(), nullable=False),
        sa.Column("stay_id", sa.Uuid(), nullable=False),
        sa.Column("guest_identity_id", sa.Uuid(), nullable=False),
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("guest_language", sa.String(length=2), nullable=False),
        sa.Column("room_number", sa.String(length=20), nullable=False),
        sa.Column("check_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guest_input_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("requisites_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("edited_fields", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column(
            "extraction_state",
            sa.Enum(
                "pending",
                "done",
                "failed",
                name="act_extraction_state",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("checked_by_display_name", sa.String(length=255), nullable=True),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_REQUESTS}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["checked_by_user_id"],
            ["users.id"],
            name=op.f(f"fk_{_REQUESTS}_checked_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_REQUESTS}")),
        sa.UniqueConstraint("request_id", name=op.f(f"uq_{_REQUESTS}_request_id")),
        sa.UniqueConstraint(
            "tenant_id", "stay_id", "client_key", name="uq_act_requests_stay_client_key"
        ),
    )
    op.create_index(op.f(f"ix_{_REQUESTS}_tenant_id"), _REQUESTS, ["tenant_id"])
    op.create_index("ix_act_requests_tenant_created", _REQUESTS, ["tenant_id", "created_at"])
    _apply_tenant_rls(_REQUESTS)

    op.create_table(
        _FILES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("act_request_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "text_state",
            sa.Enum(
                "pending",
                "text",
                "no_text",
                "image",
                "failed",
                name="act_file_text_state",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_FILES}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["act_request_id"],
            ["act_requests.id"],
            name=op.f(f"fk_{_FILES}_act_request_id_act_requests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["uploaded_files.id"],
            name=op.f(f"fk_{_FILES}_file_id_uploaded_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_FILES}")),
        sa.UniqueConstraint("file_id", name=op.f(f"uq_{_FILES}_file_id")),
        sa.UniqueConstraint(
            "act_request_id", "position", name="uq_act_request_files_request_position"
        ),
    )
    op.create_index(op.f(f"ix_{_FILES}_tenant_id"), _FILES, ["tenant_id"])
    op.create_index(op.f(f"ix_{_FILES}_act_request_id"), _FILES, ["act_request_id"])
    _apply_tenant_rls(_FILES)


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицами; связи — раньше запросов.
    op.drop_table(_FILES)
    op.drop_table(_REQUESTS)
