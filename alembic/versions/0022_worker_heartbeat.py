"""пульс воркера: worker_heartbeats

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-10

Issue #136. Таблица на одну строку: имя процесса и момент, когда его цикл
последний раз закончил круг (`shared/heartbeat.py`). По возрасту этой отметки
watchdog снаружи видит мёртвый или зависший воркер (ERR-OPS-003) — сам о своей
смерти воркер доложить не может.

Таблица НЕтенантная (как `tenants`): жив ли процесс — факт инсталляции, а не
отеля. RLS поэтому не применяется, канон 0002 сюда не копируется; DML-права
роль `hospitality_app` получает автоматически через ALTER DEFAULT PRIVILEGES
из 0002.

Строка засевается здесь с моментом применения миграции. Без засева «строки
нет» было бы неотличимо от «воркер ни разу не стартовал», и watchdog молчал бы
ровно в том случае, ради которого задача: деплой прошёл, миграции применились,
а процесс воркера не поднялся. С засевом отметка стареет с момента деплоя и
через `ALERT_WORKER_HEARTBEAT_MAX_AGE_SECONDS` даёт алерт.

Не PII (имя процесса и метка времени) — строки в docs/PII_REGISTRY.md не требует.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_worker_heartbeats")),
    )
    # Имя — константа EVENTS_WORKER из shared/heartbeat.py.
    op.execute("INSERT INTO worker_heartbeats (name, beat_at) VALUES ('events-worker', now())")


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
