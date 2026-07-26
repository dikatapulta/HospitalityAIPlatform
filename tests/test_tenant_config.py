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


# --- Маршрутизация уведомлений по службам (spec 0026, issue #80) ---


def _config_with_routing(mapping: dict[str, str]) -> TenantConfig:
    data = _valid_config_data()
    data["staff_chats_by_category"] = mapping
    return TenantConfig.model_validate(data)


def test_staff_routing_defaults_to_empty() -> None:
    """Поле аддитивное: конфиг без него читается и означает «всё в общий чат»."""
    config = TenantConfig.model_validate(_valid_config_data())
    assert config.staff_chats_by_category == {}
    assert config.staff_chat_ids(default="999") == frozenset({"999"})
    assert config.staff_chat_for("housekeeping", default="999") == "999"


def test_staff_routing_maps_category_to_its_chat() -> None:
    config = _config_with_routing({"housekeeping": "-1001", "it-support": "-1002"})
    assert config.staff_chat_for("housekeeping", default="999") == "-1001"
    assert config.staff_chat_for("it-support", default="999") == "-1002"
    # Категория без маппинга и «категории нет вовсе» — оба в дефолтный чат.
    assert config.staff_chat_for("maintenance", default="999") == "999"
    assert config.staff_chat_for(None, default="999") == "999"


def test_staff_chat_ids_collects_default_and_services() -> None:
    """Множество staff-чатов = дефолтный + чаты служб; дубли схлопываются."""
    config = _config_with_routing({"housekeeping": "-1001", "it-support": "999"})
    assert config.staff_chat_ids(default="999") == frozenset({"999", "-1001"})


def test_staff_chat_ids_drops_empty_default() -> None:
    """Ненастроенный TELEGRAM_STAFF_CHAT_ID не делает персоналом чат с пустым id."""
    config = _config_with_routing({"housekeeping": "-1001"})
    assert config.staff_chat_ids(default="") == frozenset({"-1001"})


def test_staff_routing_rejects_malformed_category_key() -> None:
    """Опечатка в ключе (Housekeeping, house_keeping) обязана падать: иначе
    настройка выглядит рабочей, а уведомления молча идут в общий чат."""
    for bad_key in ("Housekeeping", "house_keeping", "", "хозчасть"):
        with pytest.raises(ValidationError, match="category key"):
            _config_with_routing({bad_key: "-1001"})


def test_staff_routing_rejects_empty_chat_id() -> None:
    """Пустой адрес = молча выключенные уведомления службы; так не настраивают."""
    with pytest.raises(ValidationError, match="empty staff chat id"):
        _config_with_routing({"housekeeping": "   "})
