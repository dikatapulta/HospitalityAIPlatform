"""Квоты бесплатного в конфиге тенанта и в онбординге (spec 0056 §3, §7).

Схема — последний рубеж для данных отеля: цена, которую гость услышит не той
суммой, и квота со службой, которой у отеля нет, обязаны падать здесь, а не
на ходе гостя. Счёт «уже заказано» — тесты модуля requests (`test_quota.py`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hospitality.modules.room_service.api import MAX_PRICE_MINOR
from hospitality.modules.room_service.schemas import MINOR_UNITS_PER_TENGE
from hospitality.platform.config import HotelProfile, TenantConfig, load_tenant_config
from hospitality.platform.models import Tenant
from hospitality.platform.service_quotas import (
    MAX_QUOTA_UNIT_PRICE_MINOR,
    QUOTA_PRICE_MINOR_PER_TENGE,
    ServiceQuota,
)
from hospitality.shared.db import platform_session_scope
from hospitality.tools.onboard_tenant import (
    OnboardingError,
    OnboardingResult,
    TenantProfile,
    build_config,
    format_report,
    onboard_tenant,
)

_SLUG = "quota-hotel-test"


def _quota_data(
    *, category_key: str = "housekeeping", paid: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "key": "water",
        "names": {"en": "Bottled water", "ru": "Бутилированная вода"},
        "category_key": category_key,
        "per_guest_per_day": 1,
        "paid": paid,
    }


def _profile(**overrides: Any) -> TenantProfile:
    return TenantProfile.model_validate(
        {
            "city": "Almaty",
            "country_code": "KZ",
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "request_reminder_after_minutes": 30,
            "categories": [
                {"key": "housekeeping", "name": "Уборка"},
                {"key": "fnb", "name": "Room service"},
            ],
            **overrides,
        }
    )


def _config(*quotas: dict[str, Any]) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile": HotelProfile(city="Almaty", country_code="KZ"),
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "service_quotas": list(quotas),
        }
    )


# --- схема ------------------------------------------------------------------


def test_hotel_without_quotas_has_none() -> None:
    """Поле аддитивное: конфиг до spec 0056 читается как «квот нет»."""
    assert _config().service_quotas == ()


@pytest.mark.parametrize("price", [50_050, 0, MAX_QUOTA_UNIT_PRICE_MINOR + 100])
def test_paid_price_is_whole_tenge_within_ceiling(price: int) -> None:
    """Ревью PR #562: `format_amount` показывает целые тенге — 50 050 тиын гость
    услышал бы как «500 ₸»; ноль и опечатка сверх потолка — не цена."""
    with pytest.raises(ValidationError):
        _config(_quota_data(paid={"category_key": "fnb", "unit_price_minor": price}))


def test_price_rules_match_room_service_item() -> None:
    """Те же ограничения, что у цены товара (kernel модуль не импортирует, R-5)."""
    assert MAX_QUOTA_UNIT_PRICE_MINOR == MAX_PRICE_MINOR
    assert QUOTA_PRICE_MINOR_PER_TENGE == MINOR_UNITS_PER_TENGE


def test_paid_without_price_means_staff_names_it() -> None:
    config = _config(_quota_data(paid={"category_key": "fnb"}))

    paid = config.service_quotas[0].paid
    assert paid is not None
    assert paid.unit_price_minor is None


@pytest.mark.parametrize("norm", [0, 21])
def test_norm_is_one_to_twenty(norm: int) -> None:
    with pytest.raises(ValidationError):
        _config({**_quota_data(), "per_guest_per_day": norm})


def test_quota_keys_are_unique() -> None:
    """Две квоты с одним ключом считали бы одни и те же заявки дважды."""
    with pytest.raises(ValidationError, match="duplicate service quota key"):
        _config(_quota_data(), _quota_data())


# --- онбординг --------------------------------------------------------------


@pytest.mark.parametrize(
    ("quota", "service"),
    [
        (_quota_data(category_key="minibar"), "бесплатного 'minibar'"),
        (_quota_data(paid={"category_key": "bar"}), "платного 'bar'"),
    ],
)
def test_build_config_rejects_quota_service_the_hotel_does_not_have(
    quota: dict[str, Any], service: str
) -> None:
    """Иначе бесплатное или платное ушло бы в службу, которой у отеля нет."""
    with pytest.raises(OnboardingError, match=service):
        build_config(_profile(service_quotas=[quota]), reception_phone=None, previous=None)


def test_report_names_quota_and_paid_option() -> None:
    paid = {"category_key": "fnb", "unit_price_minor": 50_000}
    config = build_config(
        _profile(service_quotas=[_quota_data(paid=paid)]), reception_phone=None, previous=None
    )
    result = OnboardingResult.model_construct(
        tenant_id=uuid.uuid4(),
        tenant_created=True,
        categories_created=(),
        categories_existing=(),
        categories_extra=(),
        config=config,
    )

    report = format_report(_SLUG, result)

    assert "Квоты бесплатного: 1" in report
    assert "water: 1 на гостя в день, housekeeping; сверх — платно, fnb, 500 ₸ за штуку" in report


async def _stored_quotas() -> tuple[ServiceQuota, ...]:
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        return (await load_tenant_config(session, tenant_id)).service_quotas


@pytest.mark.usefixtures("canonical_database")
async def test_onboarding_keeps_quotas_when_profile_has_no_key() -> None:
    """Spec 0056 §3: норма пилота — только в `*.local`; прогон публичного профиля
    (без ключа) её не стирает, а явный `[]` — убирает."""
    with_quotas = _profile(service_quotas=[_quota_data()])
    await onboard_tenant(slug=_SLUG, name="Quota Hotel", profile=with_quotas, reception_phone=None)

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert await _stored_quotas() == with_quotas.service_quotas

    await onboard_tenant(
        slug=_SLUG, name=None, profile=_profile(service_quotas=[]), reception_phone=None
    )

    assert await _stored_quotas() == ()
