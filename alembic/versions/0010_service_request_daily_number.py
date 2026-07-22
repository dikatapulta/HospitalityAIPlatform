"""service_requests: daily_number, service_day + уникальность номера в дне

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-17

Дневной номер заявки `#N` (заход 2а по issue #38): человеческая метка для
глаз/речи/отчёта, сброс раз в сутки по времени отеля. Две колонки на
`service_requests`:

- `service_day` (DATE, NULL) — календарный день отеля на момент создания заявки
  (локальная дата из tz конфига тенанта, §9: в БД дата уже «свёрнута», не UTC).
- `daily_number` (INTEGER, NULL) — порядковый номер заявки в пределах этого дня.

Уникальность в паре `(tenant_id, service_day, daily_number)` — уникальный
индекс: он же защита от гонки (второй INSERT с тем же номером отвергается,
сервис пересчитывает и повторяет — см. `service.create_request`). Разные дни
могут повторять `#12`: номер не ключ действия, а метка.

Колонки NULLABLE намеренно: строки, созданные до этой миграции (staging Phase 0),
номера не получают — бэкофилл не нужен, старые заявки уже отработаны. Индекс
уникальности в Postgres допускает несколько NULL, поэтому доскелетные строки
ему не мешают.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_requests", sa.Column("service_day", sa.Date(), nullable=True))
    op.add_column("service_requests", sa.Column("daily_number", sa.Integer(), nullable=True))
    op.create_unique_constraint(
        "uq_service_requests_daily_number",
        "service_requests",
        ["tenant_id", "service_day", "daily_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_service_requests_daily_number", "service_requests", type_="unique")
    op.drop_column("service_requests", "daily_number")
    op.drop_column("service_requests", "service_day")
