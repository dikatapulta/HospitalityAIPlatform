"""Конфиг брони столика (spec 0064 §2.6, §7 п. 7): пределы ресторана, служба ресторана —
категория отеля, онбординг не стирает настроенное поле."""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hospitality.platform.config import TenantConfig, load_tenant_config
from hospitality.platform.models import Tenant
from hospitality.platform.table_booking_config import RestaurantConfig, TableBookingConfig
from hospitality.shared.db import platform_session_scope
from hospitality.tools.onboard_tenant import (
    OnboardingError,
    TenantProfile,
    build_config,
    load_profile,
    onboard_tenant,
)

_SLUG = "table-booking-hotel"


def _restaurant(**overrides: Any) -> dict[str, Any]:
    return {
        "key": "terrace",
        "names": {"en": "Terrace"},
        "category_key": "fnb",
        "first_seating": "12:00",
        "last_seating": "22:00",
        **overrides,
    }


def _profile_data(**overrides: Any) -> dict[str, Any]:
    return {
        "city": "Almaty",
        "country_code": "KZ",
        "timezone": "Asia/Almaty",
        "default_language": "ru",
        "request_reminder_after_minutes": 30,
        "categories": [{"key": "fnb", "name": "Рестораны"}],
        **overrides,
    }


def test_restaurant_hours_and_party_limits() -> None:
    restaurant = RestaurantConfig.model_validate(_restaurant())

    assert (restaurant.first_seating, restaurant.max_party) == (time(12, 0), 8)
    with pytest.raises(ValidationError, match="before first_seating"):
        RestaurantConfig.model_validate(_restaurant(first_seating="22:00", last_seating="12:00"))
    with pytest.raises(ValidationError):
        RestaurantConfig.model_validate(_restaurant(max_party=31))
    with pytest.raises(ValidationError):
        RestaurantConfig.model_validate(_restaurant(key="Terrace Bar"))
    with pytest.raises(ValidationError, match="duplicate restaurant keys"):
        TableBookingConfig.model_validate({"restaurants": [_restaurant(), _restaurant()]})


def test_open_only_when_enabled_with_a_restaurant() -> None:
    assert TableBookingConfig.model_validate(
        {"enabled": True, "restaurants": [_restaurant()]}
    ).is_open
    assert not TableBookingConfig.model_validate({"restaurants": [_restaurant()]}).is_open
    assert not TableBookingConfig(enabled=True).is_open


def test_load_profile_rejects_restaurant_with_unknown_service(tmp_path: Path) -> None:
    """§7 п. 7: служба ресторана не из категорий отеля — ошибка конфига ДО базы."""
    path = tmp_path / "profile.json"
    booking = {"enabled": True, "restaurants": [_restaurant(category_key="kitchen")]}
    path.write_text(json.dumps(_profile_data(table_booking=booking)), encoding="utf-8")

    with pytest.raises(OnboardingError, match="служба 'kitchen'"):
        load_profile(path)


def test_build_config_takes_table_booking_from_profile_or_previous() -> None:
    booking = {"enabled": True, "restaurants": [_restaurant()]}
    configured = build_config(
        TenantProfile.model_validate(_profile_data(table_booking=booking)),
        reception_phone=None,
        previous=None,
    )
    kept = build_config(
        TenantProfile.model_validate(_profile_data()), reception_phone=None, previous=configured
    )

    assert configured.table_booking is not None and configured.table_booking.is_open
    assert kept.table_booking == configured.table_booking


async def _stored_config() -> TenantConfig:
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        return await load_tenant_config(session, tenant_id)


@pytest.mark.usefixtures("canonical_database")
async def test_onboarding_keeps_table_booking_and_turns_it_off_explicitly() -> None:
    """Профиль без ключа (публичный, без ресторанов пилота) бронь не стирает;
    явный `"enabled": false` — выключает."""
    booking = {"enabled": True, "restaurants": [_restaurant()]}
    with_booking = TenantProfile.model_validate(_profile_data(table_booking=booking))
    await onboard_tenant(slug=_SLUG, name="Hotel", profile=with_booking, reception_phone=None)

    plain = TenantProfile.model_validate(_profile_data())
    await onboard_tenant(slug=_SLUG, name=None, profile=plain, reception_phone=None)
    assert (await _stored_config()).table_booking == with_booking.table_booking

    off = TenantProfile.model_validate(_profile_data(table_booking={**booking, "enabled": False}))
    await onboard_tenant(slug=_SLUG, name=None, profile=off, reception_phone=None)
    stored = (await _stored_config()).table_booking
    assert stored is not None and not stored.is_open
