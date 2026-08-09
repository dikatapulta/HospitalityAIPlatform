"""outbox: dead_lettered_at / dead_letter_alerted_at — терминальный статус

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-09

Issue #133, ADR-015. До этой миграции «повторы исчерпаны» было вычислением над
текущим конфигом (`attempts >= WORKER_MAX_DELIVERY_ATTEMPTS`), а не фактом в
строке: событие переставало доставляться молча и от «ещё в очереди» ничем не
отличалось. `dead_lettered_at` делает исход терминальным состоянием строки,
`dead_letter_alerted_at` помнит, что о нём уже сказали человеку (алерт не
повторяется на каждом цикле воркера и не теряется при его рестарте).

Бэкфилла нет намеренно: предел попыток живёт в конфиге, миграция его не знает.
Строки, исчерпавшие попытки до этой миграции, диспетчер снова возьмёт в работу
(предикат выборки теперь `dead_lettered_at IS NULL`) — успех доставит их,
неудача переведёт в dead-letter с алертом. Молчаливый остаток так рассасывается
сам, в обе стороны честно.

Индекс — обычный btree по `dead_lettered_at`, как у `processed_at` в 0003: по
нему ходят обе стороны — выборка диспетчера (`IS NULL`) и минутный прогон
алерта (`IS NOT NULL`). Partial-индекс не заводим (как и в 0003/0004): объём
Фазы 0 не требует (NG-8). `dead_letter_alerted_at` индекса не получает — он
никогда не бывает единственным условием выборки.

Не PII (метки времени доставки) — строки в docs/PII_REGISTRY.md не требует.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("dead_letter_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_outbox_events_dead_lettered_at"), "outbox_events", ["dead_lettered_at"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_events_dead_lettered_at"), table_name="outbox_events")
    op.drop_column("outbox_events", "dead_letter_alerted_at")
    op.drop_column("outbox_events", "dead_lettered_at")
