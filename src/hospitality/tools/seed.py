"""Сид демо-данных: тенант Demo Hotel, его категории заявок (Task 0011/0013) и
room service — конфиг и каталоги (spec 0038, `tools/demo_room_service.py`),
вымышленный кодекс «Наставника» (spec 0048 §10, PR 3) и факт справочника «Нужен
АВР» (spec 0049 Р-12).

Композиционный entrypoint сида: `platform/seed.py` создаёт тенанта (kernel
не может импортировать доменные модули — направление слоёв R-5), а категории
заявок и каталоги — данные модулей requests и room_service, поэтому склейка
живёт здесь, в `tools/` (слой вне контрактов, как composition root).

Идемпотентен: повторный запуск не создаёт дубликатов — существующая
категория распознаётся по ERR-REQUESTS-004 (ключ занят) и пропускается,
непустой каталог не заполняется, а кодекс засевается только тенанту без кодекса
(загруженный менеджером не трогается).
Кодекс засевается без векторов: деплой не платит модели, а векторы досчитает
первый вопрос, когда на staging включат `LLM_MODEL_MENTOR` (ADR-024 §2).

Запуск:

    make seed                          # локально (make dev поднят)
    python -m hospitality.tools.seed   # то же самое напрямую (так гоняет деплой)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
)
from hospitality.platform.config import (
    MAX_HOTEL_FACTS,
    MAX_HOTEL_FACTS_TOTAL_CHARS,
    HotelFact,
    TenantConfig,
    hotel_facts_total_chars,
    mutate_tenant_config,
)
from hospitality.platform.seed import seed_demo_tenant
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_room_service import seed_demo_room_service

logger = get_logger(module=__name__)

# Стартовый набор категорий демо-отеля (DoD Task 0013: заявка создаётся
# curl-ом на staging — категория обязана существовать заранее). `fnb` и
# `supply` — службы каталогов room service (spec 0038 Р-1), `accounting` —
# «Бухгалтерия» формы реквизитов для АВР (spec 0049 Р-7).
DEMO_CATEGORIES = (
    RequestCategoryCreate(key="housekeeping", name="Уборка номера"),
    RequestCategoryCreate(key="maintenance", name="Техническая неисправность"),
    RequestCategoryCreate(key="it-support", name="Wi-Fi и ТВ"),
    RequestCategoryCreate(key="fnb", name="Ресторан и room service"),
    RequestCategoryCreate(key="supply", name="Снабжение"),
    RequestCategoryCreate(key=ACCOUNTING_SERVICE_KEY, name="Бухгалтерия"),
)

# «Нужен АВР» — данные, а не промпт (spec 0049 Р-12): бот отвечает фактом
# справочника. Сид добавляет факт, если темы нет; удалённый руками вернётся на
# следующем деплое — это демо-отель, справочник настоящего отеля сид не трогает.
DEMO_DOCUMENTS_FACT = HotelFact(
    topic="Закрывающие документы (АВР)",
    answer=(
        "Нажмите скрепку внизу и пришлите реквизиты вашей компании файлом или текстом. "
        "Бухгалтерия подготовит АВР и счёт-фактуру к выезду."
    ),
)

# Вымышленный кодекс демо-отеля: разметка «Наставника» без данных настоящего
# отеля — репозиторий публичный.
DEMO_CODEX_PATH = Path(__file__).with_name("demo_codex.md")


async def seed_demo_data() -> None:
    """Создать/дозаполнить демо-тенанта, категории, room service и кодекс (идемпотентно)."""
    tenant_id = await seed_demo_tenant()
    with tenant_context(tenant_id):
        for category in DEMO_CATEGORIES:
            try:
                await create_category(category)
            except AppError as error:
                if error.code != ERR_REQUESTS_CATEGORY_KEY_TAKEN:
                    raise
                logger.info("demo_category_seed_skipped", category_key=category.key)
        await _seed_demo_codex()
    await seed_demo_room_service(tenant_id)
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, _with_documents_fact)


def _with_documents_fact(config: TenantConfig) -> TenantConfig:
    topics = {fact.topic.strip().casefold() for fact in config.hotel_facts}
    if DEMO_DOCUMENTS_FACT.topic.casefold() in topics:
        return config
    facts = [*config.hotel_facts, DEMO_DOCUMENTS_FACT]
    if len(facts) > MAX_HOTEL_FACTS or hotel_facts_total_chars(facts) > MAX_HOTEL_FACTS_TOTAL_CHARS:
        # Справочник заполнен руками до предела — не ломать деплой ради демо-факта.
        logger.info("demo_documents_fact_skipped_full")
        return config
    return config.model_copy(update={"hotel_facts": facts})


async def _seed_demo_codex() -> None:
    if await mentor_api.has_codex():
        logger.info("demo_codex_seed_skipped")
        return
    text = DEMO_CODEX_PATH.read_text(encoding="utf-8")
    await mentor_api.replace_codex(
        [mentor_api.CodexFile(name=DEMO_CODEX_PATH.name, text=text)],
        embeddings=None,
        embedding_model=None,
        uploaded_by_user_id=None,
    )


def main() -> None:  # pragma: no cover — точка входа; логика покрыта тестами сида
    configure_logging(get_settings().log_level)
    asyncio.run(seed_demo_data())


if __name__ == "__main__":  # pragma: no cover
    main()
