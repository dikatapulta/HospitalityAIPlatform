"""integrations/pms: последняя принятая выгрузка Opera тенанта — pms_report_feeds

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-02

Spec 0058 §2.4, §3 (issue #122, ADR-004): адаптер `OperaReportPmsAdapter`
принимает файл отчёта Opera маршрутом загрузки и хранит здесь последний
принятый снимок тенанта; файл на диск не ложится.

Строка на тенанта:
- `token_hash` — SHA-256 токена загрузки (ADR-008: секреты — только хэши);
  строку заводит выпуск токена (`python -m hospitality.tools.pms_token`),
  перевыпуск меняет хэш — старый токен гаснет;
- `source_id`, `taken_at`, `content_sha256`, `rows` — последний принятый снимок;
  пусто, пока файлов не было. `rows` — массив строк снимка (комната, номер
  брони, даты, гости): порядок держит массив, а не ключи объекта JSONB.
  Номер брони — ПД (связь с системой отеля, docs/PII_REGISTRY.md).

Шаг обратно-совместим: новая таблица старому образу не видна. Downgrade
удаляет таблицу вместе с токенами: после повторного подъёма токен
выпускается заново и передаётся IT отеля, первый же файл вернёт снимок.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

_TABLE = "pms_report_feeds"


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
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "rows",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_TABLE}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f(f"pk_{_TABLE}")),
    )
    _apply_tenant_rls(_TABLE)


def downgrade() -> None:
    # Политика и ограничения удаляются вместе с таблицей.
    op.drop_table(_TABLE)
