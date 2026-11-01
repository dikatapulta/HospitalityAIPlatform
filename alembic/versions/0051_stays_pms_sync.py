"""guests: источник Stay и состояние сверки со снимком PMS — stays.source, pms_sync_state

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-02

Spec 0058 §3 (issue #122, ADR-004): Stay заводит и ведёт сверка со снимком
отчёта Opera рядом с ручным заселением кабинета.

- `stays.source` — `manual` | `pms`, VARCHAR(16) без CHECK состава (канон
  `origin`, миграция 0025); `server_default 'manual'` остаётся навсегда: старый
  образ в окне деплоя и после отката заселяет, не зная колонки;
- `stays.pms_booking_ref` — номер брони в Opera (ПД — связь с системой отеля,
  docs/PII_REGISTRY.md); partial UNIQUE среди активных Stay тенанта и обычный
  индекс по той же паре — «у брони уже был Stay, заново не заводить» (§2.2);
- CHECK: номер брони есть ровно у `source = 'pms'` — старый образ его не
  нарушит, он не пишет ни одной из колонок;
- `stays.pms_missed_snapshots` — сколько применённых снимков подряд Stay в них
  нет (выезд — со второго, §2.2);
- `pms_sync_state` — строка на тенанта: какой снимок применён, придержанные
  выезды, список расхождений для кабинета, эпизод алерта «снимок устарел».

Таблица принятых файлов адаптера Opera (`pms_report_feeds`) — своей миграцией
вместе с адаптером (PR 4 серии): без адаптера она пустая и ничья.

Шаг обратно-совместим: у новых колонок умолчания, новая таблица старому образу
не видна. Мешают откату только данные — строки журнала `stay_pms_*`
(docs/runbooks/deploy.md, часть C). Downgrade снимает колонки и таблицу: Stay
остаются, но все — ручные. После повторного подъёма сверка снова свяжет их с
бронями по дате заезда (§2.2); не совпала — расхождение в кабинете, дублей нет.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

_STAYS = "stays"
_STATE = "pms_sync_state"
_CHECK = f"ck_{_STAYS}_pms_booking_ref_only_pms"
_UNIQUE_ACTIVE = "uq_stays_tenant_pms_booking_checked_in"
_INDEX = "ix_stays_tenant_pms_booking_ref"


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
    op.add_column(
        _STAYS,
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
    )
    op.add_column(_STAYS, sa.Column("pms_booking_ref", sa.String(length=64), nullable=True))
    op.add_column(
        _STAYS,
        sa.Column("pms_missed_snapshots", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f(_CHECK), _STAYS, "(source = 'pms') = (pms_booking_ref IS NOT NULL)"
    )
    op.create_index(
        _UNIQUE_ACTIVE,
        _STAYS,
        ["tenant_id", "pms_booking_ref"],
        unique=True,
        postgresql_where=sa.text("status = 'checked_in'"),
    )
    op.create_index(_INDEX, _STAYS, ["tenant_id", "pms_booking_ref"])

    op.create_table(
        _STATE,
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("last_applied_source_id", sa.Uuid(), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("held_closures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "discrepancies",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("stale_alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_STATE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f(f"pk_{_STATE}")),
    )
    _apply_tenant_rls(_STATE)


def downgrade() -> None:
    # Политика и ограничения удаляются вместе с таблицей.
    op.drop_table(_STATE)
    op.drop_index(_INDEX, table_name=_STAYS)
    op.drop_index(_UNIQUE_ACTIVE, table_name=_STAYS)
    op.drop_constraint(op.f(_CHECK), _STAYS, type_="check")
    op.drop_column(_STAYS, "pms_missed_snapshots")
    op.drop_column(_STAYS, "pms_booking_ref")
    op.drop_column(_STAYS, "source")
