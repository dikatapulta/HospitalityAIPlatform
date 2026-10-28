"""Схемы room service в конфиге тенанта (spec 0038 Р-6, Р-10, §3): `LocalizedText`,
`RoomServiceConfig` и правило «каталог открыт». Без БД — только схемы kernel.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    RoomServiceCatalogConfig,
    RoomServiceConfig,
    TenantConfig,
)


def _tenant_config(room_service: RoomServiceConfig | None) -> TenantConfig:
    return TenantConfig(
        profile=HotelProfile(city="Almaty", country_code="KZ"),
        timezone="Asia/Almaty",
        default_language="ru",
        room_service=room_service,
    )


def _restaurant(percent: Any = "10") -> RoomServiceCatalogConfig:
    return RoomServiceCatalogConfig(category_key="fnb", service_charge_percent=percent)


def test_localized_text_requires_english() -> None:
    for raw in ({"ru": "Сырники"}, {"en": "  ", "ru": "Сырники"}):
        with pytest.raises(ValidationError, match="English text is required|Field required"):
            LocalizedText.model_validate(raw)


def test_localized_text_rejects_foreign_language() -> None:
    """Перевод на язык, которого платформа не показывает, не пропадает молча."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LocalizedText.model_validate({"en": "Tea", "de": "Tee"})


def test_localized_text_blank_translation_falls_back_to_english() -> None:
    text = LocalizedText.model_validate({"en": " Black tea ", "ru": "Чёрный чай", "kk": "  "})

    assert text.kk is None
    assert text.as_json() == {"en": "Black tea", "ru": "Чёрный чай"}
    assert (text.text_for("ru"), text.text_for("kk"), text.text_for("uz")) == (
        "Чёрный чай",
        "Black tea",
        "Black tea",
    )
    assert LocalizedText.model_validate(text.as_json()) == text


@pytest.mark.parametrize("languages", [(), ("ru", "en"), ("en", "de"), ("en", "ru", "ru")], ids=str)
def test_room_service_languages_start_with_english(languages: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        RoomServiceConfig(languages=languages, catalogs={"restaurant": _restaurant()})


@pytest.mark.parametrize("percent", ["-1", "30.01", "10.005"])
def test_service_charge_percent_is_bounded(percent: str) -> None:
    with pytest.raises(ValidationError):
        _restaurant(percent)


def test_unknown_catalog_and_bad_category_key_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RoomServiceConfig.model_validate({"catalogs": {"bar": _restaurant().model_dump()}})
    with pytest.raises(ValidationError):
        RoomServiceCatalogConfig(category_key="F&B", service_charge_percent=Decimal(0))


def test_catalog_is_open_only_when_enabled_and_configured() -> None:
    enabled = RoomServiceConfig(enabled=True, catalogs={"restaurant": _restaurant()})

    assert _tenant_config(None).room_service_catalog("restaurant") is None
    assert (
        _tenant_config(enabled.model_copy(update={"enabled": False})).room_service_catalog(
            "restaurant"
        )
        is None
    )
    assert _tenant_config(enabled).room_service_catalog("shop") is None
    assert _tenant_config(enabled).room_service_catalog("restaurant") == _restaurant()


def test_room_service_survives_jsonb_round_trip() -> None:
    """Конфиг пишется `model_dump(mode="json")` и читается заново на каждом запросе."""
    config = _tenant_config(
        RoomServiceConfig(
            enabled=True,
            catalogs={
                "restaurant": RoomServiceCatalogConfig(
                    category_key="fnb",
                    service_charge_percent=Decimal("12.5"),
                    delivery_note=LocalizedText(en="30–40 min", ru="30–40 мин"),
                )
            },
        )
    )

    assert TenantConfig.model_validate(config.model_dump(mode="json")) == config


def test_tenant_config_without_room_service_still_validates() -> None:
    """Поле аддитивное (§6): конфиги до spec 0038 ключа не знают."""
    raw = _tenant_config(None).model_dump(mode="json")
    del raw["room_service"]

    assert TenantConfig.model_validate(raw).room_service is None
