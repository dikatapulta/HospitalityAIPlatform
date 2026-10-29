"""Тесты сида демо-данных: тенант + конфиг (Task 0011), категории заявок (Task 0013).

DoD задач: сид идемпотентен (повторный запуск не дублирует и не перезаписывает),
конфиг читается сервисом через канонический `load_tenant_config`.
Нужен работающий Postgres (`make dev`) — как у всех DB-тестов (conftest).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests.api import list_categories
from hospitality.modules.room_service.api import RoomServiceCatalog, get_catalog, get_settings
from hospitality.platform.config import (
    TENANT_CONFIG_INVALID_ERROR_CODE,
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TENANT_NOT_FOUND_ERROR_CODE,
    HotelFact,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.platform.seed import DEMO_TENANT_SLUG, demo_tenant_config, seed_demo_tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_room_service import DEMO_ROOM_SERVICE
from hospitality.tools.seed import DEMO_CATEGORIES, DEMO_DOCUMENTS_FACT, seed_demo_data

pytestmark = pytest.mark.usefixtures("canonical_database")


async def _demo_tenant_count() -> int:
    async with platform_session_scope() as session:
        count = await session.scalar(
            select(func.count()).select_from(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG)
        )
    return count or 0


async def test_seed_creates_demo_tenant_with_readable_config() -> None:
    """DoD: после сида демо-тенант существует и конфиг читается сервисом."""
    tenant_id = await seed_demo_tenant()

    assert await _demo_tenant_count() == 1
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config == demo_tenant_config()
    assert config.timezone == "Asia/Almaty"


async def test_seed_is_idempotent() -> None:
    """DoD: повторный запуск не дублирует тенанта."""
    first_id = await seed_demo_tenant()
    second_id = await seed_demo_tenant()

    assert first_id == second_id
    assert await _demo_tenant_count() == 1


async def test_seed_does_not_overwrite_existing_config() -> None:
    """Правки конфига (руками/онбордингом) переживают повторный сид на деплое."""
    tenant_id = await seed_demo_tenant()
    customized = demo_tenant_config().model_copy(update={"default_language": "kk"})
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, customized)

    await seed_demo_tenant()

    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config.default_language == "kk"


async def test_seed_fills_config_of_unconfigured_demo_tenant() -> None:
    """Тенант, созданный до Task 0011 без конфига, дозаполняется, а не дублируется."""
    async with platform_session_scope() as session:
        bare_tenant = Tenant(slug=DEMO_TENANT_SLUG, name="Demo Hotel")
        session.add(bare_tenant)
        await session.flush()
        bare_tenant_id = bare_tenant.id

    seeded_id = await seed_demo_tenant()

    assert seeded_id == bare_tenant_id
    assert await _demo_tenant_count() == 1
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, bare_tenant_id)
    assert config == demo_tenant_config()


async def test_seed_demo_data_creates_categories_idempotently() -> None:
    """DoD Task 0013: после сида у демо-тенанта есть категории для заявок;
    повторный запуск (каждый деплой staging) не создаёт дубликатов."""
    await seed_demo_data()
    tenant_id = await seed_demo_tenant()  # тот же тенант, id для контекста

    with tenant_context(tenant_id):
        first_run = await list_categories()
    assert [category.key for category in first_run] == sorted(
        category.key for category in DEMO_CATEGORIES
    )

    await seed_demo_data()

    with tenant_context(tenant_id):
        second_run = await list_categories()
    assert [category.id for category in second_run] == [category.id for category in first_run]


async def _catalog_snapshot(tenant_id: uuid.UUID) -> dict[RoomServiceCatalog, list[uuid.UUID]]:
    with tenant_context(tenant_id):
        return {
            catalog: [
                item.id
                for section in (await get_catalog(catalog, include_hidden=True)).sections
                for item in section.items
            ]
            for catalog in RoomServiceCatalog
        }


async def test_seed_demo_data_fills_room_service_idempotently() -> None:
    """spec 0038 §6 PR 3: оба каталога, «принимаем», службы каталогов — категории
    демо-отеля; повторный сид (каждый деплой) не дублирует товары."""
    await seed_demo_data()
    tenant_id = await seed_demo_tenant()

    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config.room_service == DEMO_ROOM_SERVICE
    first = await _catalog_snapshot(tenant_id)
    assert all(first.values())
    category_keys = {category.key for category in DEMO_CATEGORIES}
    with tenant_context(tenant_id):
        for catalog in RoomServiceCatalog:
            settings = await get_settings(catalog)
            assert settings.accepting_orders is True
            assert settings.category_key in category_keys

    await seed_demo_data()

    assert await _catalog_snapshot(tenant_id) == first


async def test_seed_adds_documents_fact_once() -> None:
    """spec 0049 Р-12: «нужен АВР» — факт справочника демо-отеля; повторный сид
    (каждый деплой) не дублирует его и не трогает факты, написанные руками."""
    tenant_id = await seed_demo_tenant()
    own = HotelFact(topic="Wi-Fi", answer="Сеть Demo, пароль на карточке.")
    async with platform_session_scope() as session:
        await store_tenant_config(
            session, tenant_id, demo_tenant_config().model_copy(update={"hotel_facts": [own]})
        )

    await seed_demo_data()
    await seed_demo_data()

    async with platform_session_scope() as session:
        facts = (await load_tenant_config(session, tenant_id)).hotel_facts
    assert facts == [own, DEMO_DOCUMENTS_FACT]
    assert "скрепку" in DEMO_DOCUMENTS_FACT.answer


async def test_seed_keeps_room_service_turned_off_by_hand() -> None:
    """Выключенный руками room service сид не включает обратно и не падает на нём."""
    tenant_id = await seed_demo_tenant()
    turned_off = DEMO_ROOM_SERVICE.model_copy(update={"enabled": False})
    async with platform_session_scope() as session:
        await store_tenant_config(
            session, tenant_id, demo_tenant_config().model_copy(update={"room_service": turned_off})
        )

    await seed_demo_data()

    async with platform_session_scope() as session:
        assert (await load_tenant_config(session, tenant_id)).room_service == turned_off


async def test_load_config_of_unknown_tenant_raises_not_found() -> None:
    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, uuid.uuid4())
    assert error.value.code == TENANT_NOT_FOUND_ERROR_CODE


async def test_load_config_of_unconfigured_tenant_raises() -> None:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="bare-hotel", name="Bare Hotel")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, tenant_id)
    assert error.value.code == TENANT_NOT_CONFIGURED_ERROR_CODE


async def test_load_config_failing_schema_raises_invalid() -> None:
    """Дрейф данных (конфиг в БД не проходит схему) — явная ошибка, не молчание."""
    tenant_id = await seed_demo_tenant()
    async with platform_session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        # Нарочно мимо store_tenant_config: моделируем несовместимый дрейф.
        tenant.config = {"schema_version": 999}

    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await load_tenant_config(session, tenant_id)
    assert error.value.code == TENANT_CONFIG_INVALID_ERROR_CODE


async def test_store_config_for_unknown_tenant_raises_not_found() -> None:
    async with platform_session_scope() as session:
        with pytest.raises(AppError) as error:
            await store_tenant_config(session, uuid.uuid4(), demo_tenant_config())
    assert error.value.code == TENANT_NOT_FOUND_ERROR_CODE


async def test_stored_config_roundtrips_through_schema() -> None:
    """store → load возвращает эквивалентную модель (JSONB не теряет форму)."""
    tenant_id = await seed_demo_tenant()
    updated = TenantConfig.model_validate(
        {
            "profile": {"city": "Astana", "country_code": "KZ"},
            "timezone": "Asia/Almaty",
            "default_language": "en",
        }
    )
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, updated)

    async with platform_session_scope() as session:
        assert await load_tenant_config(session, tenant_id) == updated


async def test_seed_adds_demo_codex_once_and_keeps_an_uploaded_one() -> None:
    """Spec 0048 §10: вымышленный кодекс для staging — без векторов (деплой не
    платит модели), а загруженный менеджером кодекс повторный сид не трогает."""
    await seed_demo_data()
    tenant_id = await seed_demo_tenant()
    with tenant_context(tenant_id):
        info = await mentor_api.get_codex()
        assert info is not None
        assert info.title == "Кодекс гостиницы «Demo Hotel»"
        assert info.article_count == 7
        pending = await mentor_api.list_articles_needing_embedding("text-embedding-3-small")
        assert len(pending) == 7

        uploaded = [
            mentor_api.CodexFile(
                name="k.md",
                text="Название: Свой\n# Часть\n"
                "## Раздел 1. Р\n### Глава 1. Г\n#### Статья 1. С\nТекст.\nИсточник: свой.\n",
            )
        ]
        await mentor_api.replace_codex(
            uploaded, embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
    await seed_demo_data()
    with tenant_context(tenant_id):
        info = await mentor_api.get_codex()
    assert info is not None and info.title == "Свой"
