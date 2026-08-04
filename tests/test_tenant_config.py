"""Тесты схемы конфигурации тенанта (Task 0011, FOUNDATION §6, P-7).

В основном чистая валидация без БД; чтение/запись конфига через БД —
tests/test_seed.py. Исключение — `list_configured_tenant_ids` (spec 0028):
это запрос к реестру тенантов, и проверять его иначе как на БД нечем.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from hospitality.platform.config import (
    DEFAULT_REQUEST_REMINDER_MINUTES,
    MAX_CATEGORY_HINT_LENGTH,
    TENANT_CONFIG_SCHEMA_VERSION,
    TenantConfig,
    list_configured_tenant_ids,
)
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope


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


def _config_with_reminders(
    after_minutes: int | None = 30, by_category: dict[str, int] | None = None
) -> TenantConfig:
    data = _valid_config_data()
    data["request_reminder_after_minutes"] = after_minutes
    data["request_reminder_minutes_by_category"] = by_category or {}
    return TenantConfig.model_validate(data)


def test_reminder_delay_defaults_to_platform_value() -> None:
    """Поле аддитивное: конфиг без него читается и означает платформенные 30 мин
    (spec 0028: отель, который ничего не настроил, обязан получать защиту)."""
    config = TenantConfig.model_validate(_valid_config_data())
    assert config.request_reminder_after_minutes == DEFAULT_REQUEST_REMINDER_MINUTES
    assert config.request_reminder_minutes_by_category == {}
    assert config.reminder_delay_for("housekeeping") == timedelta(minutes=30)
    assert config.min_reminder_delay() == timedelta(minutes=30)


def test_reminder_delay_can_be_switched_off() -> None:
    """`null` — явное «напоминаний у этого отеля нет»."""
    config = _config_with_reminders(after_minutes=None)
    assert config.reminder_delay_for("housekeeping") is None
    assert config.reminder_delay_for(None) is None
    assert config.min_reminder_delay() is None


def test_category_reminder_delay_overrides_base() -> None:
    """Уборка ≠ прорыв трубы: свой срок категории перекрывает базовый."""
    config = _config_with_reminders(after_minutes=30, by_category={"maintenance": 10})
    assert config.reminder_delay_for("maintenance") == timedelta(minutes=10)
    assert config.reminder_delay_for("housekeeping") == timedelta(minutes=30)
    # Незнакомая категория — базовый срок: заявка реальна, даже если категория
    # не резолвится.
    assert config.reminder_delay_for(None) == timedelta(minutes=30)
    # Граница выборки кандидатов — самый ранний срок отеля.
    assert config.min_reminder_delay() == timedelta(minutes=10)


def test_category_reminder_works_without_base_delay() -> None:
    """«Напоминаем только про инженерию» — валидная настройка: пер-категорийный
    срок работает и при выключенном базовом."""
    config = _config_with_reminders(after_minutes=None, by_category={"maintenance": 10})
    assert config.reminder_delay_for("maintenance") == timedelta(minutes=10)
    assert config.reminder_delay_for("housekeeping") is None
    assert config.reminder_delay_for(None) is None
    assert config.min_reminder_delay() == timedelta(minutes=10)


def test_reminder_minutes_out_of_range_rejected() -> None:
    """Границы 1..10080 — на ОБОИХ полях: `0` означал бы шум вместо сигнала,
    а «5000000» — опечатку, выглядящую рабочей настройкой."""
    for bad_minutes in (0, -5, 10081):
        with pytest.raises(ValidationError):
            _config_with_reminders(after_minutes=bad_minutes)
        with pytest.raises(ValidationError, match="between"):
            _config_with_reminders(by_category={"maintenance": bad_minutes})


def test_reminder_minutes_reject_malformed_category_key() -> None:
    """Та же строгость к ключу, что у маршрутизации чатов (spec 0026): опечатка
    обязана падать, иначе служба ждёт напоминаний, которых не будет."""
    for bad_key in ("Maintenance", "main_tenance", "", "инженерия"):
        with pytest.raises(ValidationError, match="category key"):
            _config_with_reminders(by_category={bad_key: 10})


def _config_with_hints(hints: dict[str, str]) -> TenantConfig:
    data = _valid_config_data()
    data["category_hints"] = hints
    return TenantConfig.model_validate(data)


def test_category_hints_default_to_empty() -> None:
    """Поле аддитивное: конфиг без него читается как есть (§6)."""
    assert TenantConfig.model_validate(_valid_config_data()).category_hints == {}


def test_category_hints_keep_ambiguous_item_in_two_services() -> None:
    """Смысл подсказок (#123): «кофе» намеренно стоит у двух служб — по нему
    модель видит неоднозначность и спрашивает гостя, а не угадывает."""
    config = _config_with_hints(
        {"housekeeping": "кофе в пакетиках, вода", "fnb": "сваренный кофе, платная вода"}
    )
    assert [key for key, hint in config.category_hints.items() if "кофе" in hint] == [
        "housekeeping",
        "fnb",
    ]


def test_category_hints_reject_malformed_category_key() -> None:
    """Та же строгость к ключу, что у чатов и сроков: опечатка обязана падать —
    иначе подсказка службы молча не доедет до инструмента."""
    for bad_key in ("Housekeeping", "house_keeping", "", "хозчасть"):
        with pytest.raises(ValidationError, match="category key"):
            _config_with_hints({bad_key: "кофе в пакетиках"})


def test_category_hints_reject_empty_hint() -> None:
    """Пустая подсказка — «подсказки нет», и выражается отсутствием ключа:
    иначе модель прочтёт пустую строку как «сюда ничего не относится»."""
    with pytest.raises(ValidationError, match="empty hint"):
        _config_with_hints({"housekeeping": "   "})


def test_category_hints_reject_too_long_hint() -> None:
    """Подсказка уходит в описание инструмента каждым ходом — это токены на
    каждом сообщении гостя, а не место для должностной инструкции."""
    with pytest.raises(ValidationError, match="longer than"):
        _config_with_hints({"housekeeping": "к" * (MAX_CATEGORY_HINT_LENGTH + 1)})


async def test_list_configured_tenant_ids_skips_onboarding_incomplete(
    canonical_database: None,
) -> None:
    """Обход фоновых задач (spec 0028): тенант без конфига — онбординг не
    завершён, срока и адресата у него нет, поэтому в список он не попадает."""
    async with platform_session_scope() as session:
        configured = Tenant(
            slug="configured-hotel",
            name="Configured",
            config=TenantConfig.model_validate(_valid_config_data()).model_dump(mode="json"),
        )
        bare = Tenant(slug="bare-hotel", name="Bare")
        session.add_all([configured, bare])
        await session.flush()

        tenant_ids = await list_configured_tenant_ids(session)

    assert configured.id in tenant_ids
    assert bare.id not in tenant_ids
