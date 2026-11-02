"""integrations/pms: свободные номера по ночам в pms_report_feeds

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-02

Spec 0059 §2.1, §3 (issue #234): второй файл того же отеля — на каждую ночь
ближайших 30…62 дней число номеров, свободных для продажи. Адаптер Opera
принимает его маршрутом `opera-availability` и хранит последний принятый файл
в той же строке тенанта, рядом со снимком «кто в доме»:
- `availability_taken_at` — момент приёма (UTC); пусто, пока файлов не было;
- `availability_sha256` — хэш файла: тот же файл в пределах минуты — повтор;
- `availability_nights` — массив `{night, free_rooms}`: порядок ночей держит
  массив, а не ключи объекта JSONB.

Броней, комнат и гостей в файле нет — это не ПД (docs/PII_REGISTRY.md не
меняется). Таблица уже тенантная, RLS не меняется.

Шаг обратно-совместим: старый образ новых колонок не читает, у новых —
NULL и `'[]'` по умолчанию. Downgrade удаляет колонки вместе с последним
файлом: бот о продлении ведёт себя как v1, пока не придёт следующий файл
(раз в 15 минут).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

_TABLE = "pms_report_feeds"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("availability_taken_at", sa.DateTime(timezone=True)))
    op.add_column(_TABLE, sa.Column("availability_sha256", sa.String(length=64)))
    op.add_column(
        _TABLE,
        sa.Column(
            "availability_nights",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "availability_nights")
    op.drop_column(_TABLE, "availability_sha256")
    op.drop_column(_TABLE, "availability_taken_at")
