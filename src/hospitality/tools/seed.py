"""Сид демо-данных: тенант Demo Hotel, его категории заявок (Task 0011/0013) и
room service — конфиг, каталоги и стоковые фото товаров на staging (spec 0038, #794,
`tools/demo_room_service.py`),
вымышленный кодекс «Наставника» (spec 0048 §10, PR 3), факт справочника «Нужен
АВР» (spec 0049 Р-12), выдуманные рестораны брони столика с её фактом (spec 0064,
`tools/demo_table_booking.py`) и вымышленное здание 3D-витрины со службами продаж и
бронирования (spec 0069, `tools/demo_showroom.py`); имена служб — языком отеля (#833);
под `STAGING_ALL_FEATURES` — все флаги разделов демо-отеля (#940).

Композиционный entrypoint сида: `platform/seed.py` создаёт тенанта (kernel
не может импортировать доменные модули — направление слоёв R-5), а категории
заявок и каталоги — данные модулей requests и room_service, поэтому склейка
живёт здесь, в `tools/` (слой вне контрактов, как composition root).

Идемпотентен: повторный запуск не создаёт дубликатов — существующая
категория распознаётся по ERR-REQUESTS-004 (ключ занят) и пропускается
(прежнее имя по умолчанию на staging меняется на имя языком отеля, #833 —
`_rename_demo_categories`),
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
import uuid
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from hospitality.integrations.file_storage import build_file_storage
from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
    rename_category,
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
from hospitality.shared.ports.file_storage import set_file_storage
from hospitality.shared.staging_features import staging_models_without_key
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_room_service import seed_demo_room_service
from hospitality.tools.demo_showcase import staging_refusals
from hospitality.tools.demo_showroom import DEMO_SHOWROOM_CATEGORIES, seed_demo_showroom
from hospitality.tools.demo_table_booking import seed_demo_table_booking

logger = get_logger(module=__name__)

# Стартовый набор категорий демо-отеля (DoD Task 0013: заявка создаётся
# curl-ом на staging — категория обязана существовать заранее). `fnb` и
# `supply` — службы каталогов room service (spec 0038 Р-1), `accounting` —
# «Бухгалтерия» формы реквизитов для АВР (spec 0049 Р-7). Имена — языком отеля
# (#833): так отделы зовут в отеле; гостю бот службу не называет.
DEMO_CATEGORIES = (
    RequestCategoryCreate(key="housekeeping", name="Хаускипинг"),
    RequestCategoryCreate(key="maintenance", name="Техническая неисправность"),
    RequestCategoryCreate(key="it-support", name="IT"),
    RequestCategoryCreate(key="fnb", name="F&B"),
    RequestCategoryCreate(key="supply", name="Снабжение"),
    RequestCategoryCreate(key=ACCOUNTING_SERVICE_KEY, name="Бухгалтерия"),
)

# Прежние имена по умолчанию (до #833) по ключу. Созданную службу сид не трогает,
# поэтому на staging демо-отеля имя меняется отдельным шагом — только если оно
# всё ещё прежнее: имя, поменянное руками, остаётся.
DEMO_CATEGORY_FORMER_NAMES: Final = {
    "housekeeping": "Уборка номера",
    "it-support": "Wi-Fi и ТВ",
    "fnb": "Ресторан и room service",
    "reservations": "Бронирование",
}

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
    """Создать/дозаполнить демо-тенанта, категории, room service, кодекс и бронь столика
    (идемпотентно)."""
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
    await seed_demo_table_booking(tenant_id)
    await seed_demo_showroom(tenant_id)
    with tenant_context(tenant_id):
        await _rename_demo_categories()
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, _with_documents_fact)
    await _enable_demo_sections(tenant_id)


async def _enable_demo_sections(tenant_id: uuid.UUID) -> None:
    """Под `STAGING_ALL_FEATURES` — все разделы демо-отеля (#940, `shared/staging_features.py`).

    Вторая защита — защиты демо-инструментов (`staging_refusals`): выключатель по
    ошибке в `.env` копии отеля флаги её демо-тенанта всё равно не тронет.
    """
    settings = get_settings()
    if not settings.staging_all_features:
        return
    refusals = staging_refusals(confirmed=True)
    if refusals:
        logger.warning("staging_all_features_tenant_skipped", reasons=refusals)
        return
    for name in staging_models_without_key(settings):
        logger.warning("staging_all_features_model_without_key", setting=name.upper())
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, _with_all_sections)


def _with_all_sections(config: TenantConfig) -> TenantConfig:
    """Каждый флаг раздела `*_enabled` — включён, если его зависимости на месте.

    Зависимость флага — валидатор `TenantConfig` (у `guest_feedback_enabled` —
    служба эскалаций): не прошёл — флаг остаётся выключенным, причина — в логе
    деплоя; остальные разделы включаются.
    """
    for name, field in TenantConfig.model_fields.items():
        if not name.endswith("_enabled") or field.annotation is not bool or getattr(config, name):
            continue
        try:
            config = TenantConfig.model_validate({**config.model_dump(mode="json"), name: True})
        except ValidationError as error:
            reason = error.errors()[0]["msg"]
            logger.warning("staging_section_flag_skipped", flag=name, reason=reason)
            continue
        logger.info("staging_section_flag_enabled", flag=name)
    return config


async def _rename_demo_categories() -> None:
    """Прежние имена служб — на имена языком отеля (#833), только на staging демо-отеля
    (защиты демо-инструментов, `demo_showcase.staging_refusals`)."""
    if staging_refusals(confirmed=True):
        logger.info("demo_category_rename_skipped_not_staging")
        return
    names = {
        category.key: category.name for category in (*DEMO_CATEGORIES, *DEMO_SHOWROOM_CATEGORIES)
    }
    for key, former_name in DEMO_CATEGORY_FORMER_NAMES.items():
        await rename_category(key, expected_name=former_name, new_name=names[key])


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
    settings = get_settings()
    configure_logging(settings.log_level)
    # Фото товаров демо-отеля (#794) идут каноном приёма файла — ему нужен
    # адаптер хранилища; кладёт его composition root, а сид запускается сам по себе.
    set_file_storage(build_file_storage(settings))
    asyncio.run(seed_demo_data())


if __name__ == "__main__":  # pragma: no cover
    main()
