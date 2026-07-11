"""Тесты схемы конфигурации тенанта (Task 0011, FOUNDATION §6, P-7).

Чистая валидация без БД; чтение/запись конфига через БД — tests/test_seed.py.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from hospitality.platform.config import (
    TENANT_CONFIG_SCHEMA_VERSION,
    TenantConfig,
)


def _valid_config_data() -> dict[str, Any]:
    return {
        "schema_version": TENANT_CONFIG_SCHEMA_VERSION,
        "profile": {"city": "Almaty", "country_code": "KZ"},
        "timezone": "Asia/Almaty",
        "default_language": "ru",
    }


def test_valid_config_passes_schema() -> None:
    config = TenantConfig.model_validate(_valid_config_data())
    assert config.schema_version == TENANT_CONFIG_SCHEMA_VERSION
    assert config.profile.city == "Almaty"
    assert config.default_language == "ru"


def test_tzinfo_returns_hotel_timezone() -> None:
    """Канон времени §9: локальное время отеля — из конфига тенанта."""
    config = TenantConfig.model_validate(_valid_config_data())
    assert config.tzinfo == ZoneInfo("Asia/Almaty")


def test_unknown_timezone_rejected() -> None:
    data = _valid_config_data()
    data["timezone"] = "Almaty/Nonexistent"
    with pytest.raises(ValidationError, match="IANA"):
        TenantConfig.model_validate(data)


def test_wrong_schema_version_rejected() -> None:
    """§6: конфиг чужой версии не принимается молча — нужен скрипт миграции."""
    data = _valid_config_data()
    data["schema_version"] = TENANT_CONFIG_SCHEMA_VERSION + 1
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(data)


def test_unknown_field_rejected() -> None:
    """extra='forbid': опечатка в имени поля — ошибка, а не молчание."""
    data = _valid_config_data()
    data["defualt_language"] = "kk"
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(data)


def test_invalid_language_code_rejected() -> None:
    data = _valid_config_data()
    data["default_language"] = "russian"
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(data)


def test_invalid_country_code_rejected() -> None:
    data = _valid_config_data()
    data["profile"] = {"city": "Almaty", "country_code": "KAZ"}
    with pytest.raises(ValidationError):
        TenantConfig.model_validate(data)


def test_config_is_frozen() -> None:
    """Конфиг — значение: менять только целиком через store_tenant_config."""
    config = TenantConfig.model_validate(_valid_config_data())
    with pytest.raises(ValidationError):
        config.timezone = "Europe/Berlin"
