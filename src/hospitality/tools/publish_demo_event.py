"""Сквозная проверка конвейера событий (Task 0010 DoD, runbook deploy.md).

Публикует каноническое событие `canary.created` от имени служебного тенанта
`demo-smoke` (создаётся при первом запуске): бизнес-запись (канарейка) и
событие уходят одной транзакцией; работающий воркер доставит событие
подписчику `echo_canary_created`. След виден в логах app и worker с общим
correlation id — команда печатает его в лог-событии `demo_event_published`.

Запуск — в контейнере приложения (там же, где деплой гоняет миграции):

    docker compose ... exec app python -m hospitality.tools.publish_demo_event
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from sqlalchemy import select

from hospitality.platform.events import CanaryCreated
from hospitality.platform.models import Tenant, TenantIsolationCanary
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.events import publish
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context

DEMO_TENANT_SLUG = "demo-smoke"

logger = get_logger(module=__name__)


async def publish_demo_event() -> str:
    """Публикует демо-событие; возвращает correlation id для поиска следа в логах."""
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == DEMO_TENANT_SLUG))
        if tenant_id is None:
            demo_tenant = Tenant(slug=DEMO_TENANT_SLUG, name="Demo Smoke")
            session.add(demo_tenant)
            await session.flush()
            tenant_id = demo_tenant.id

    correlation_id = str(uuid.uuid4())
    with (
        structlog.contextvars.bound_contextvars(correlation_id=correlation_id),
        tenant_context(tenant_id),
    ):
        # Канон публикации (P-6): бизнес-запись и событие — одна транзакция.
        async with session_scope() as session:
            canary = TenantIsolationCanary(note=f"demo:{utc_now().isoformat()}")
            session.add(canary)
            await session.flush()  # canary.id нужен полезной нагрузке события
            await publish(session, CanaryCreated(canary_id=canary.id, note=canary.note))
        logger.info("demo_event_published", demo_tenant_slug=DEMO_TENANT_SLUG)
    return correlation_id


def main() -> None:  # pragma: no cover — точка входа; логика покрыта тестом воркера
    configure_logging(get_settings().log_level)
    asyncio.run(publish_demo_event())


if __name__ == "__main__":  # pragma: no cover
    main()
