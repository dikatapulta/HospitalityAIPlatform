"""housekeeping: room_turnovers — путь номера после выезда до «готов к заселению»

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-25

Spec 0046, ADR-021 (issue #476): ресепшен отмечает выезд по номеру комнаты,
хаускипинг проверяет, убирает, старший принимает. Кабинет ведёт работу, а не
статус номера — тот остаётся в OPERA (NG-2). Своя тенантная таблица модуля
`housekeeping`: уборка после выезда — не заявка (ADR-021).

Partial UNIQUE `(tenant_id, room_number)` по открытым статусам — один открытый
путь на номер: две одновременные отметки выезда не создадут двух строк. Текст
условия — тот же, что `OPEN_TURNOVER_STATUSES_SQL` модели.

Шаг обратно-совместим целиком: одна новая таблица, существующие не тронуты.
Старый образ в окне деплоя её не видит. Downgrade удаляет таблицу вместе с
путями — после отката их показывать некому.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_TABLE = "room_turnovers"
# Шаги пути, у каждого — «когда», «кто» (FK на users) и снапшот имени.
_STEP_PREFIXES = ("inspected", "cleaning_started", "cleaned", "cancelled")


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


def _actor_columns(prefix: str) -> list[sa.Column[Any]]:
    return [
        sa.Column(f"{prefix}_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(f"{prefix}_by_display_name", sa.String(length=255), nullable=True),
    ]


def _actor_fk(prefix: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [f"{prefix}_by_user_id"],
        ["users.id"],
        name=op.f(f"fk_{_TABLE}_{prefix}_by_user_id_users"),
        ondelete="SET NULL",
    )


def upgrade() -> None:
    step_columns: list[sa.Column[Any]] = []
    for prefix in _STEP_PREFIXES:
        step_columns.append(sa.Column(f"{prefix}_at", sa.DateTime(timezone=True), nullable=True))
        step_columns.extend(_actor_columns(prefix))
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("room_number", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("inspection_note", sa.String(length=500), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        *_actor_columns("reported"),
        *step_columns,
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        *_actor_columns("accepted"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        *(_actor_fk(prefix) for prefix in ("reported", *_STEP_PREFIXES, "accepted")),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(op.f(f"ix_{_TABLE}_tenant_id"), _TABLE, ["tenant_id"])
    op.create_index(
        "uq_room_turnovers_tenant_room_open",
        _TABLE,
        ["tenant_id", "room_number"],
        unique=True,
        postgresql_where=sa.text("status IN ('reported', 'inspected', 'cleaning', 'cleaned')"),
    )
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_TABLE)
