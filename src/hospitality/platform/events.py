"""События модуля platform (Task 0010, P-6): канонический пример.

CANONICAL: каждый новый модуль копирует этот паттерн в свой `events.py`
(анатомия §5.2): класс события — наследник `DomainEvent` с `event_name` и
типизированной нагрузкой; подписчик — идемпотентная async-функция. Пары
(событие, обработчик) регистрирует composition root воркера
(`hospitality/worker.py`), сами модули друг о друге не знают.

`CanaryCreated` привязан к канонической тенантной таблице
`tenant_isolation_canary` (Task 0009) и служит сквозной проверкой конвейера
событий на staging (`hospitality/tools/publish_demo_event.py`, runbook deploy).
Первое настоящее бизнес-событие появится с модулем requests (Task 0012).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from sqlalchemy import func, select

from hospitality.platform.models import TenantIsolationCanary
from hospitality.shared.db import session_scope
from hospitality.shared.events import DomainEvent
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class CanaryCreated(DomainEvent):
    """CANONICAL: образец доменного события — факт «создана канарейка»."""

    event_name: ClassVar[str] = "canary.created"

    canary_id: uuid.UUID
    note: str


async def echo_canary_created(event: CanaryCreated) -> None:
    """CANONICAL: образец идемпотентного подписчика (P-8).

    Идемпотентность — через естественный ключ эффекта (`echo:<canary_id>`):
    повторная доставка того же события находит уже созданный эффект и выходит.
    Подписчик выполняется в `tenant_context` события, поэтому `session_scope`
    здесь — обычный тенантный канон, без специальных приседаний.
    """
    echo_note = f"echo:{event.canary_id}"
    async with session_scope() as session:
        already_echoed = await session.scalar(
            select(func.count())
            .select_from(TenantIsolationCanary)
            .where(TenantIsolationCanary.note == echo_note)
        )
        if already_echoed:
            logger.info("canary_echo_skipped", canary_id=str(event.canary_id))
            return
        session.add(TenantIsolationCanary(note=echo_note))
    logger.info("canary_echoed", canary_id=str(event.canary_id))
