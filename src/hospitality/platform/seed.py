"""Сид демо-тенанта «Demo Hotel» (Task 0011, PHASE0).

На демо-тенанте тестируются все последующие задачи фазы; его часовой пояс —
опора канона времени (§9: в БД UTC, локальное время — из конфига тенанта).

Идемпотентен: тенанта нет — создаёт с конфигом; тенант есть без конфига —
дозаполняет; тенант с конфигом — не трогает (правки конфига руками или
онбордингом переживают повторные запуски: сид выполняется на каждом деплое
staging, см. ops/deploy/deploy.sh). Параллельный запуск не создаст дубликат —
защищает уникальность `tenants.slug`; проигравший упадёт на IntegrityError,
повторный запуск пройдёт.

Запуск:

    make seed                                 # локально (make dev поднят)
    python -m hospitality.platform.seed       # то же самое напрямую
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import configure_logging, get_logger

DEMO_TENANT_SLUG = "demo-hotel"
DEMO_TENANT_NAME = "Demo Hotel"

logger = get_logger(module=__name__)


def demo_tenant_config() -> TenantConfig:
    """Конфиг демо-тенанта: Алматы — целевой рынок Фазы 0."""
    return TenantConfig(
        profile=HotelProfile(city="Almaty", country_code="KZ"),
        timezone="Asia/Almaty",
        default_language="ru",
    )


async def seed_demo_tenant() -> uuid.UUID:
    """Создать/дозаполнить демо-тенанта; вернуть его id (идемпотентно)."""
    async with platform_session_scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG))
        if tenant is None:
            tenant = Tenant(
                slug=DEMO_TENANT_SLUG,
                name=DEMO_TENANT_NAME,
                config=demo_tenant_config().model_dump(mode="json"),
            )
            session.add(tenant)
            await session.flush()  # tenant.id нужен для лога и возврата
            logger.info("demo_tenant_created", tenant_id=str(tenant.id), slug=DEMO_TENANT_SLUG)
        elif tenant.config is None:
            await store_tenant_config(session, tenant.id, demo_tenant_config())
            logger.info(
                "demo_tenant_config_filled", tenant_id=str(tenant.id), slug=DEMO_TENANT_SLUG
            )
        else:
            logger.info("demo_tenant_seed_skipped", tenant_id=str(tenant.id), slug=DEMO_TENANT_SLUG)
        return tenant.id


def main() -> None:  # pragma: no cover — точка входа; логика покрыта тестами сида
    configure_logging(get_settings().log_level)
    asyncio.run(seed_demo_tenant())


if __name__ == "__main__":  # pragma: no cover
    main()
