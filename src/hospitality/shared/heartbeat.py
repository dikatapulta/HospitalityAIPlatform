"""Пульс воркера: отметка «цикл жив», по которой снаружи виден мёртвый процесс.

Issue #136. Упавший или зависший воркер не детектится ничем: API отвечает,
`/health/ready` зелёный, а события копятся в outbox — уведомления службам,
напоминания и эскалации молча не идут. О собственной смерти воркер доложить не
может по построению, поэтому он оставляет отметку времени каждым кругом цикла,
а сравнивает её с порогом и алертит ДРУГОЙ процесс — watchdog
(`tools/alerter.py`, ERR-OPS-003), которому она видна метрикой
``worker_heartbeat_age_seconds`` в `/metrics`.

**Почему Postgres, а не Redis.** Отметка обязана пережить ровно тот отказ, о
котором алерт. В Redis она теряется при его рестарте, и «пульса нет» становится
неотличимо от «воркер ни разу не стартовал»: после перезагрузки сервера с
воркером, который не поднялся, watchdog молчал бы — то самое молчание, против
которого задача. В БД отметка живёт: «последний пульс 40 минут назад» остаётся
правдой через рестарт чего угодно. Цена — одна запись на круг — снята тем же
приёмом, что у остальных периодических шагов воркера: пульс обновляется не чаще
``worker_heartbeat_interval_seconds`` (30 с), то есть 2 UPDATE в минуту по
первичному ключу.

Таблица НЕтенантная (как `tenants`): жив ли процесс — факт инсталляции, а не
отеля; RLS к ней неприменим, читается и пишется платформенной сессией.
Строку `events-worker` заводит миграция 0022 с моментом её применения — тогда
«воркер не стартовал после деплоя» тоже стареет и тоже алертит.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, platform_session_scope, utc_now

# Имя пульса единственного сегодня воркера (ADR-005: процесс один). Второй
# процесс со своим циклом заведёт свою строку — ключ на то и имя, а не флаг.
EVENTS_WORKER = "events-worker"


class WorkerHeartbeat(Base):
    """Строка пульса: «процесс с этим именем закончил круг цикла в этот момент»."""

    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(UTCDateTime())


async def record_worker_heartbeat(name: str = EVENTS_WORKER) -> None:
    """Отметить круг цикла: upsert одной строки (зовёт `run_worker`).

    Upsert, а не UPDATE: строка живёт с миграции, но восстановление БД из
    бэкапа или ручное удаление не имеют права оставить воркер без пульса
    навсегда — идемпотентная запись сама себя чинит (P-8).
    """
    async with platform_session_scope() as session:
        beat_at = utc_now()
        await session.execute(
            insert(WorkerHeartbeat)
            .values(name=name, beat_at=beat_at)
            .on_conflict_do_update(index_elements=["name"], set_={"beat_at": beat_at})
        )


async def read_heartbeat_age_seconds(name: str = EVENTS_WORKER) -> float | None:
    """Сколько секунд назад был последний пульс; None — строки пульса нет.

    None означает «данных нет», а не «воркер жив»: и метрика, и watchdog
    трактуют его как «не знаю» и молчат (см. `_evaluate_worker_heartbeat`).
    После миграции 0022 это состояние недостижимо — остаётся только как
    честный ответ на удалённую руками строку.

    Возраст считается в Python по `utc_now()`, как аренда доставки в
    `events._claim_batch`: процессы инсталляции делят часы хоста.
    """
    async with platform_session_scope() as session:
        beat_at = await session.scalar(
            select(WorkerHeartbeat.beat_at).where(WorkerHeartbeat.name == name)
        )
    if beat_at is None:
        return None
    return (utc_now() - beat_at).total_seconds()
