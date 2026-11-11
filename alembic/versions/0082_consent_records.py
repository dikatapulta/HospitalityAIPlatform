"""guests: запись о согласии гостя, переживающая проживание

Revision ID: 0082
Revises: 0080
Create Date: 2026-11-11

Spec 0032 §6 (issue #948): данные о проживании удаляются через 90 дней после
выезда, а доказательство согласия нужно три года. `consent_records` — журнал
«только вставка»: канал, момент и версия согласия, комната и даты проживания
(у согласия Telegram до ввода кода — NULL). Имени и идентификатора чата нет.
RLS по канону 0002. Индекс по `consent_at` — под удаление по сроку.

Бэкфилл: согласия, данные до миграции, стоят на сессиях (`guest_sessions`) и
диалогах (`conversations`) и уйдут вместе с ними — каждое получает строку здесь.
Комната и даты сессии — из её Stay на момент миграции, канал — вид идентичности.

Обратно-совместима: старый образ новой таблицы не видит. Downgrade стирает
записи о согласии целиком.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0080"
branch_labels = None
depends_on = None

_TABLE = "consent_records"


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


def _backfill_tenant_table(sql: str) -> None:
    """КАНОН (копия из миграции 0002): тихий `INSERT 0` под RLS — в громкую ошибку."""
    op.execute("SET LOCAL row_security = off")
    op.execute(sql)
    op.execute("SET LOCAL row_security = on")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_version", sa.String(length=16), nullable=False),
        sa.Column("room_number", sa.String(length=20), nullable=True),
        sa.Column("check_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(op.f(f"ix_{_TABLE}_consent_at"), _TABLE, ["consent_at"])
    _apply_tenant_rls(_TABLE)

    _backfill_tenant_table(
        f"""
        INSERT INTO {_TABLE} (id, tenant_id, channel, consent_at, consent_version,
                              room_number, check_in_at, check_out_at)
        SELECT gen_random_uuid(), guest_sessions.tenant_id, guest_identities.kind,
               guest_sessions.consent_at, guest_sessions.consent_version,
               stays.room_number, stays.check_in_at, stays.check_out_at
        FROM guest_sessions
        JOIN stays ON stays.id = guest_sessions.stay_id
        JOIN guest_identities ON guest_identities.id = guest_sessions.guest_identity_id
        """
    )
    _backfill_tenant_table(
        f"""
        INSERT INTO {_TABLE} (id, tenant_id, channel, consent_at, consent_version)
        SELECT gen_random_uuid(), tenant_id, channel, consent_at, consent_version
        FROM conversations
        WHERE consent_at IS NOT NULL AND consent_version IS NOT NULL
        """
    )


def downgrade() -> None:
    # Политика, индексы и FK удаляются вместе с таблицей.
    op.drop_table(_TABLE)
