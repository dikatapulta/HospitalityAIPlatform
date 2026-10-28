"""Сид демо-данных: тенант Demo Hotel, его категории заявок (Task 0011/0013) и
room service — конфиг и каталоги (spec 0038, `tools/demo_room_service.py`).

Композиционный entrypoint сида: `platform/seed.py` создаёт тенанта (kernel
не может импортировать доменные модули — направление слоёв R-5), а категории
заявок и каталоги — данные модулей requests и room_service, поэтому склейка
живёт здесь, в `tools/` (слой вне контрактов, как composition root).

Идемпотентен: повторный запуск не создаёт дубликатов — существующая
категория распознаётся по ERR-REQUESTS-004 (ключ занят) и пропускается,
непустой каталог не заполняется.

Запуск:

    make seed                          # локально (make dev поднят)
    python -m hospitality.tools.seed   # то же самое напрямую (так гоняет деплой)
"""

from __future__ import annotations

import asyncio

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
)
from hospitality.platform.seed import seed_demo_tenant
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_room_service import seed_demo_room_service

logger = get_logger(module=__name__)

# Стартовый набор категорий демо-отеля (DoD Task 0013: заявка создаётся
# curl-ом на staging — категория обязана существовать заранее). `fnb` и
# `supply` — службы каталогов room service (spec 0038 Р-1).
DEMO_CATEGORIES = (
    RequestCategoryCreate(key="housekeeping", name="Уборка номера"),
    RequestCategoryCreate(key="maintenance", name="Техническая неисправность"),
    RequestCategoryCreate(key="it-support", name="Wi-Fi и ТВ"),
    RequestCategoryCreate(key="fnb", name="Ресторан и room service"),
    RequestCategoryCreate(key="supply", name="Снабжение"),
)


async def seed_demo_data() -> None:
    """Создать/дозаполнить демо-тенанта, его категории и room service (идемпотентно)."""
    tenant_id = await seed_demo_tenant()
    with tenant_context(tenant_id):
        for category in DEMO_CATEGORIES:
            try:
                await create_category(category)
            except AppError as error:
                if error.code != ERR_REQUESTS_CATEGORY_KEY_TAKEN:
                    raise
                logger.info("demo_category_seed_skipped", category_key=category.key)
    await seed_demo_room_service(tenant_id)


def main() -> None:  # pragma: no cover — точка входа; логика покрыта тестами сида
    configure_logging(get_settings().log_level)
    asyncio.run(seed_demo_data())


if __name__ == "__main__":  # pragma: no cover
    main()
