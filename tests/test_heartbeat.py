"""Пульс воркера (issue #136): запись отметки и её возраст.

Здесь — само хранилище (`shared/heartbeat.py`); что цикл воркера зовёт его
каждым кругом — в test_worker.py, что возраст виден наружу метрикой — в
test_metrics.py, что по метрике приходит алерт — в test_alerter.py.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.heartbeat import (
    EVENTS_WORKER,
    WorkerHeartbeat,
    read_heartbeat_age_seconds,
    record_worker_heartbeat,
)


async def test_migration_seeds_heartbeat_row(canonical_database: None) -> None:
    """Строка засеяна миграцией 0022: «воркер ни разу не стартовал» обязано
    стареть и алертить, а не выглядеть как «данных нет» (и молчать)."""
    age = await read_heartbeat_age_seconds()

    assert age is not None and age >= 0


async def test_record_updates_the_same_row(canonical_database: None) -> None:
    """Пульс — одна строка на процесс: круги цикла не копятся в таблице."""
    await record_worker_heartbeat()
    await record_worker_heartbeat()

    async with platform_session_scope() as session:
        rows = list((await session.execute(select(WorkerHeartbeat))).scalars().all())

    age = await read_heartbeat_age_seconds()

    assert [row.name for row in rows] == [EVENTS_WORKER]
    assert age is not None and age < 5.0


async def test_record_restores_deleted_row(canonical_database: None) -> None:
    """Upsert, а не UPDATE: восстановление БД из бэкапа или ручное удаление
    строки не имеют права оставить воркер без пульса навсегда (P-8)."""
    async with platform_session_scope() as session:
        row = (await session.execute(select(WorkerHeartbeat))).scalar_one()
        await session.delete(row)

    assert await read_heartbeat_age_seconds() is None  # «не знаю» — алерта не будет

    await record_worker_heartbeat()

    assert await read_heartbeat_age_seconds() is not None


async def test_age_grows_from_the_stored_moment(canonical_database: None) -> None:
    """Возраст считается от отметки: мёртвый воркер — это старая строка."""
    stale_at = utc_now() - timedelta(minutes=7)
    async with platform_session_scope() as session:
        row = (await session.execute(select(WorkerHeartbeat))).scalar_one()
        row.beat_at = stale_at

    age = await read_heartbeat_age_seconds()

    assert age is not None and 7 * 60 <= age < 7 * 60 + 60
