"""Сид по контуру: флаги разделов демо-отеля под `STAGING_ALL_FEATURES` (#940) и
демо-разделы не на копии отеля (#1053).

Флаг с невыполненной зависимостью остаётся выключенным, остальные включаются; без
выключателя или вне staging сид флаги не трогает. На копии отеля сид создаёт только
демо-тенанта и его службы — без кодекса, room service, брони столика, витрины и фактов.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.modules.mentor import api as mentor_api
from hospitality.modules.requests.api import list_categories
from hospitality.platform.config import TenantConfig, load_tenant_config, mutate_tenant_config
from hospitality.platform.seed import demo_tenant_config, seed_demo_tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools import seed
from hospitality.tools.seed import DEMO_CATEGORIES, seed_demo_data

pytestmark = pytest.mark.usefixtures("canonical_database")

SECTION_FLAGS = sorted(
    name
    for name, field in TenantConfig.model_fields.items()
    if name.endswith("_enabled") and field.annotation is bool
)


@pytest.fixture
def staging_switch() -> Iterator[None]:
    """`STAGING_ALL_FEATURES=true` только на время теста — кэш настроек не утекает."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("STAGING_ALL_FEATURES", "true")
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def on_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Защиты демо-инструментов пройдены — как на staging демо-отеля.

    Аргументом теста, а не `usefixtures`: иначе `monkeypatch` поднимется раньше
    временной БД и вернёт `POSTGRES_DB` уже после её удаления.
    """
    monkeypatch.setattr(seed, "staging_refusals", lambda *, confirmed: [])


@pytest.fixture
def hotel_copy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Признаки гостевой копии отеля; строку SENTRY_ENVIRONMENT=pilot забыли (#389).

    Аргументом теста — по той же причине, что `on_staging`.
    """
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("TELEGRAM_TENANT_SLUG", "pilot-hotel")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _flags(tenant_id: uuid.UUID) -> dict[str, bool]:
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    return {name: getattr(config, name) for name in SECTION_FLAGS}


async def _with_escalation_service(tenant_id: uuid.UUID) -> None:
    async with platform_session_scope() as session:
        await mutate_tenant_config(
            session,
            tenant_id,
            lambda config: config.model_copy(update={"escalation_category_key": "maintenance"}),
        )


def test_known_section_flags_are_covered() -> None:
    assert {"mentor_enabled", "ideas_enabled", "guest_feedback_enabled"} <= set(SECTION_FLAGS)


async def test_switch_enables_every_section_flag_of_demo_hotel(
    staging_switch: None, on_staging: None
) -> None:
    tenant_id = await seed_demo_tenant()
    await _with_escalation_service(tenant_id)

    await seed_demo_data()

    assert await _flags(tenant_id) == dict.fromkeys(SECTION_FLAGS, True)


async def test_flag_with_missing_dependency_stays_off_others_on(
    staging_switch: None, on_staging: None
) -> None:
    await seed_demo_data()  # у демо-отеля из сида нет службы эскалаций
    tenant_id = await seed_demo_tenant()

    flags = await _flags(tenant_id)
    assert flags.pop("guest_feedback_enabled") is False
    assert flags == dict.fromkeys(flags, True)


async def test_without_switch_seed_leaves_flags_off(on_staging: None) -> None:
    await seed_demo_data()

    assert await _flags(await seed_demo_tenant()) == dict.fromkeys(SECTION_FLAGS, False)


async def test_switch_off_staging_leaves_flags_off(staging_switch: None) -> None:
    # Выключатель попал в .env копии отеля — защиты демо-инструментов его не пускают.
    await seed_demo_data()

    assert await _flags(await seed_demo_tenant()) == dict.fromkeys(SECTION_FLAGS, False)


async def test_seed_on_hotel_copy_creates_only_tenant_and_services(hotel_copy: None) -> None:
    await seed_demo_data()

    tenant_id = await seed_demo_tenant()
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config == demo_tenant_config()  # ни разделов, ни фактов, ни города демо
    with tenant_context(tenant_id):
        keys = {category.key for category in await list_categories()}
        assert not await mentor_api.has_codex()
    assert keys == {category.key for category in DEMO_CATEGORIES}


@pytest.mark.parametrize(
    "contour",
    [
        {"SENTRY_ENVIRONMENT": "dev"},  # машина разработчика и CI
        {
            "SENTRY_ENVIRONMENT": "staging",
            "TELEGRAM_TENANT_SLUG": "demo-hotel",
            "PUBLIC_BASE_URL": "https://staging.example.test",
        },
    ],
)
def test_demo_sections_are_seeded_on_developer_machine_and_staging(
    monkeypatch: pytest.MonkeyPatch, contour: dict[str, str]
) -> None:
    for name, value in contour.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        assert seed.demo_sections_refusals() == []
    finally:
        get_settings.cache_clear()
