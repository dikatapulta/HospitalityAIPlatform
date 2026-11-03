"""Витрина в инструментах (spec 0069 §2.2, §2.5, §7 п. 3 и 6): онбординг переносит
настройки и код доступа, команда кода не печатает код, демо-сид идемпотентен."""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hospitality.modules.requests import api as requests_api
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.config import (
    HotelProfile,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools import showroom_access_code
from hospitality.tools.demo_showroom import seed_demo_showroom
from hospitality.tools.onboard_tenant import OnboardingError, TenantProfile, build_config
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    canonical_database,
    migrated_database_name,
)


def _profile(**overrides: Any) -> TenantProfile:
    return TenantProfile.model_validate(
        {
            "city": "Almaty",
            "country_code": "KZ",
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "request_reminder_after_minutes": 30,
            "categories": [{"key": "sales", "name": "Отдел продаж"}],
            **overrides,
        }
    )


def test_onboarding_keeps_showroom_and_its_access_code() -> None:
    previous = build_config(
        _profile(showroom={"enabled": True, "sales_category_key": "sales"}),
        reception_phone=None,
        previous=None,
    )
    assert previous.showroom is not None
    with_code = previous.model_copy(
        update={
            "showroom": previous.showroom.model_copy(
                update={"access_code_hash": "$2b$12$hash", "access_code_version": 3}
            )
        }
    )
    # Профиль без ключа — витрина целиком прежняя.
    kept = build_config(_profile(), reception_phone=None, previous=with_code)
    assert kept.showroom == with_code.showroom
    # Профиль с витриной — флаги из профиля, код — прежний.
    rebuilt = build_config(
        _profile(showroom={"enabled": False, "sales_category_key": "sales"}),
        reception_phone=None,
        previous=with_code,
    )
    assert rebuilt.showroom is not None
    assert rebuilt.showroom.enabled is False
    assert (rebuilt.showroom.access_code_hash, rebuilt.showroom.access_code_version) == (
        "$2b$12$hash",
        3,
    )


def test_onboarding_refuses_code_in_profile_and_unknown_service() -> None:
    with pytest.raises(OnboardingError, match="код доступа файлом профиля не задаётся"):
        build_config(
            _profile(
                showroom={
                    "enabled": True,
                    "sales_category_key": "sales",
                    "access_code_hash": "$2b$12$hash",
                }
            ),
            reception_phone=None,
            previous=None,
        )
    with pytest.raises(OnboardingError, match="reservations"):
        build_config(
            _profile(
                showroom={
                    "enabled": True,
                    "sales_category_key": "sales",
                    "booking_category_key": "reservations",
                }
            ),
            reception_phone=None,
            previous=None,
        )


async def _configured_tenant(slug: str) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=f"Hotel {slug}")
        session.add(tenant)
        await session.flush()
        await store_tenant_config(
            session,
            tenant.id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
            ),
        )
        return tenant.id


async def test_access_code_command_never_prints_the_code(
    canonical_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    await seed_demo_showroom(await _configured_tenant("code-hotel"))
    await _configured_tenant("no-showroom")

    assert await showroom_access_code.set_for_slug("code-hotel", "secret-show-code") == 1
    with pytest.raises(showroom_access_code.AccessCodeError, match="не найден"):
        await showroom_access_code.set_for_slug("nobody", "secret-show-code")
    with pytest.raises(showroom_access_code.AccessCodeError, match="от 8"):
        await showroom_access_code.set_for_slug("code-hotel", "short")
    with pytest.raises(showroom_access_code.AccessCodeError, match="не настроена"):
        await showroom_access_code.set_for_slug("no-showroom", "secret-show-code")
    captured = capsys.readouterr()
    assert "secret-show-code" not in captured.out + captured.err


async def test_demo_seed_is_idempotent(canonical_database: None) -> None:
    tenant_id = await _configured_tenant("seed-hotel")
    await seed_demo_showroom(tenant_id)
    await seed_demo_showroom(tenant_id)
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    assert config.showroom == ShowroomConfig(
        enabled=True, sales_category_key="sales", booking_category_key="reservations"
    )
    with tenant_context(tenant_id):
        scene = await showroom_api.get_scene()
        keys = {category.key for category in await requests_api.list_categories()}
    assert scene is not None and scene.scene_version == 1
    assert {"sales", "reservations"} <= keys
