"""Тесты схемы конфигурации тенанта (Task 0011, FOUNDATION §6, P-7).

В основном чистая валидация без БД; чтение/запись конфига через БД —
tests/test_seed.py. Исключение — `list_configured_tenant_ids` (spec 0028):
это запрос к реестру тенантов, и проверять его иначе как на БД нечем.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from hospitality.platform.config import (
    DEFAULT_REQUEST_REMINDER_MINUTES,
    MAX_CATEGORY_HINT_LENGTH,
    MAX_HOTEL_FACT_ANSWER_LENGTH,
    MAX_HOTEL_FACT_TOPIC_LENGTH,
    MAX_HOTEL_FACTS,
    MAX_HOTEL_FACTS_TOTAL_CHARS,
    TENANT_CONFIG_SCHEMA_VERSION,
    TenantConfig,
    hotel_facts_total_chars,
    list_configured_tenant_ids,
    load_tenant_config,
    mutate_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError


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


def test_staff_chat_ids_include_the_daily_summary_chat() -> None:
    """Чат утренней сводки — персонал: там сидит руководство отеля (issue #301).

    Не будь его в множестве, реплай менеджера на сводку («а что за просрочки?»)
    ушёл бы в гостевую ветку: экран согласия гостя в группе руководства и ответ
    консьержа. Чат сводки задан — он персонал; не задан (`None`) — множество не
    меняется.
    """
    data = _valid_config_data()
    data["daily_summary_chat_id"] = "-1005"
    config = TenantConfig.model_validate(data)
    assert config.staff_chat_ids(default="999") == frozenset({"999", "-1005"})

    data.pop("daily_summary_chat_id")
    assert TenantConfig.model_validate(data).staff_chat_ids(default="999") == frozenset({"999"})


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


def test_daily_summary_defaults_to_nine_in_the_morning_and_no_chat() -> None:
    """Умолчания сводки дня (spec 0035 §8): рассылка в 09:00, чат не задан.

    Отель, которого никто не настраивал, сообщений не получает — но и ошибки не
    даёт: страница кабинета у него работает, а адресата рассылки просто нет.
    """
    config = TenantConfig.model_validate(_valid_config_data())
    assert config.daily_summary_chat_id is None
    assert config.daily_summary_local_time == "09:00"
    assert config.daily_summary_at == time(9, 0)


def test_daily_summary_time_is_parsed_once() -> None:
    """«HH:MM» разбирается свойством, а не каждым читающим: строка в JSONB
    читается глазами, а прогон воркера сравнивает уже `time`."""
    config = TenantConfig.model_validate(
        {**_valid_config_data(), "daily_summary_local_time": "07:45"}
    )
    assert config.daily_summary_at == time(7, 45)


def test_daily_summary_time_rejects_anything_but_hh_mm() -> None:
    """Опечатка во времени обязана падать на схеме.

    Иначе она ляжет в БД и всплывёт в 09:00 у воркера — там её увидит не тот,
    кто её сделал, и не тогда, когда сможет исправить.
    """
    for bad_time in ("9:00", "24:00", "09:60", "0900", "утром", ""):
        with pytest.raises(ValidationError):
            TenantConfig.model_validate(
                {**_valid_config_data(), "daily_summary_local_time": bad_time}
            )


def test_daily_summary_chat_rejects_a_blank_string() -> None:
    """Выключение рассылки — `null`, а не пустая строка (канон чатов служб).

    Пробельный чат выглядел бы настроенным, а `sendMessage` уходил бы в никуда
    каждое утро.
    """
    for blank in ("", "   "):
        with pytest.raises(ValidationError):
            TenantConfig.model_validate({**_valid_config_data(), "daily_summary_chat_id": blank})


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


# --- Справочник отеля: факты в конфиге (spec 0036 §3, issue #333) ---


def _fact(topic: str, answer: str = "ответ", valid_until: str | None = None) -> dict[str, Any]:
    return {"topic": topic, "answer": answer, "valid_until": valid_until}


def _config_with_facts(facts: list[dict[str, Any]]) -> TenantConfig:
    return TenantConfig.model_validate({**_valid_config_data(), "hotel_facts": facts})


def test_hotel_facts_default_to_empty() -> None:
    """Поле аддитивное: конфиг без него читается, справочника у отеля просто нет."""
    config = TenantConfig.model_validate(_valid_config_data())
    assert config.hotel_facts == []
    assert config.active_hotel_facts() == ()


def test_hotel_facts_keep_their_order() -> None:
    """Порядок расставляет отель — и он обязан пережить схему (§3: список, не словарь)."""
    config = _config_with_facts([_fact("Wi-Fi"), _fact("Завтрак"), _fact("Бассейн")])
    assert [fact.topic for fact in config.hotel_facts] == ["Wi-Fi", "Завтрак", "Бассейн"]


def test_hotel_facts_count_limit() -> None:
    _config_with_facts([_fact(f"Тема {i}") for i in range(MAX_HOTEL_FACTS)])
    with pytest.raises(ValidationError, match="hotel facts limit"):
        _config_with_facts([_fact(f"Тема {i}") for i in range(MAX_HOTEL_FACTS + 1)])


def test_hotel_fact_topic_and_answer_length_limits() -> None:
    with pytest.raises(ValidationError):
        _config_with_facts([_fact("Т" * (MAX_HOTEL_FACT_TOPIC_LENGTH + 1))])
    with pytest.raises(ValidationError):
        _config_with_facts([_fact("Завтрак", "о" * (MAX_HOTEL_FACT_ANSWER_LENGTH + 1))])


def test_hotel_facts_total_chars_limit() -> None:
    """Бюджет промпта (§4): справочник уходит в КАЖДЫЙ ход, поэтому предел суммарный.

    Двадцать фактов по 300 знаков проходят пределы поштучно и всё равно
    удваивают цену каждой реплики гостя — ловит только сумма.
    """
    over_budget = [
        _fact(f"Тема {i}", "о" * MAX_HOTEL_FACT_ANSWER_LENGTH)
        for i in range(MAX_HOTEL_FACTS_TOTAL_CHARS // MAX_HOTEL_FACT_ANSWER_LENGTH + 1)
    ]
    with pytest.raises(ValidationError, match="hotel facts budget"):
        _config_with_facts(over_budget)
    assert hotel_facts_total_chars(_config_with_facts([_fact("Wi-Fi", "пароль")]).hotel_facts) == 11


def test_hotel_fact_topics_are_unique_ignoring_case_and_spaces() -> None:
    """Идентичность факта — тема (§3): два «Wi-Fi» молча спорили бы в промпте."""
    with pytest.raises(ValidationError, match="duplicate hotel fact topic"):
        _config_with_facts([_fact("Wi-Fi"), _fact("  wi-fi ")])


def test_hotel_fact_topic_rejects_slash() -> None:
    """«/» в теме запрещён жанром заголовка (§3), а не маршрутом (тема едет телом)."""
    with pytest.raises(ValidationError, match="must not contain"):
        _config_with_facts([_fact("Обмен валюты / банкомат")])


def test_hotel_fact_rejects_blank_topic_and_answer() -> None:
    """Канон полей конфига: «нет ответа» — это отсутствие факта, а не пустая строка."""
    for blank in ("", "   "):
        with pytest.raises(ValidationError):
            _config_with_facts([_fact(blank)])
        with pytest.raises(ValidationError):
            _config_with_facts([_fact("Завтрак", blank)])


def test_expired_hotel_fact_still_passes_the_schema() -> None:
    """Дата в прошлом схемой НЕ отвергается — и это главный инвариант поля.

    Просроченный факт остаётся в конфиге (стирать написанное отелем нельзя),
    поэтому схема, отвергающая прошлое, перестала бы пропускать конфиг —
    отель получал бы ERR-PLATFORM-006 на следующий день после срока, то есть
    бот замолчал бы целиком из-за одного истёкшего «бассейн закрыт до 15.08».
    Опечатку в годе ловит страница кабинета при сохранении (§7, §8.1).
    """
    config = _config_with_facts([_fact("Бассейн", "закрыт", valid_until="2020-01-01")])
    assert config.hotel_facts[0].valid_until == date(2020, 1, 1)
    assert config.active_hotel_facts() == ()


def test_active_hotel_facts_count_the_last_day_in_the_hotels_timezone() -> None:
    """Последний день действия — включительно, и граница берётся по поясу отеля.

    Проверяем ровно тот момент, где пояс решает: 20:00 UTC 15 августа — это уже
    01:00 16 августа в Алматы (UTC+5), значит факт «до 15.08» гость уже не
    видит, хотя по UTC день ещё не кончился.
    """
    config = _config_with_facts(
        [_fact("Бассейн", "закрыт на ремонт", valid_until="2026-08-15"), _fact("Wi-Fi")]
    )
    midday = datetime(2026, 8, 15, 6, 0, tzinfo=UTC)  # 11:00 в Алматы, последний день
    assert [fact.topic for fact in config.active_hotel_facts(now=midday)] == ["Бассейн", "Wi-Fi"]

    after_midnight_in_almaty = datetime(2026, 8, 15, 20, 0, tzinfo=UTC)
    assert [fact.topic for fact in config.active_hotel_facts(now=after_midnight_in_almaty)] == [
        "Wi-Fi"
    ]


# --- Атомарная правка конфига (spec 0036 §7): БД, а не чистая валидация ---


def _appending_fact(topic: str) -> Callable[[TenantConfig], TenantConfig]:
    """Правка «дописать факт» — та же форма, что у страницы кабинета (шаг 3)."""

    def mutate(config: TenantConfig) -> TenantConfig:
        payload = config.model_dump(mode="json")
        payload["hotel_facts"] = [*payload["hotel_facts"], _fact(topic)]
        return TenantConfig.model_validate(payload)

    return mutate


async def _tenant_with_facts(slug: str, facts: list[dict[str, Any]]) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        await store_tenant_config(session, tenant.id, _config_with_facts(facts))
        return tenant.id


async def test_hotel_facts_keep_their_order_through_the_database(
    canonical_database: None,
) -> None:
    """Порядок фактов переживает запись и чтение (§3: JSONB переставляет ключи ОБЪЕКТА).

    Темы подобраны так, что любая нормализация — по длине или побайтно — дала
    бы другой порядок: если бы факты хранились словарём, тест бы это увидел.
    """
    stored = ["Бассейн", "Wi-Fi", "Завтрак и обед"]
    tenant_id = await _tenant_with_facts("order-hotel", [_fact(topic) for topic in stored])

    async with platform_session_scope() as session:
        loaded = await load_tenant_config(session, tenant_id)

    assert [fact.topic for fact in loaded.hotel_facts] == stored


async def _wait_until_a_backend_blocks(*, timeout_seconds: float = 5.0) -> bool:
    """Дождаться, пока другое соединение этой БД встанет на блокировку строки.

    Нужно, чтобы чередование двух правок было ЗАДАНО, а не выпало случайно:
    без этой синхронизации быстрая правка успевает закончиться раньше, чем
    вторая начнётся, и тест зеленеет одинаково с блокировкой и без неё
    (проверено: `asyncio.gather` из двух правок проходит и на коде без
    `FOR UPDATE` — то есть ничего не проверяет).
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        # Своя транзакция на каждый виток: статистика бэкендов кэшируется на
        # время транзакции, и опрос внутри одной видел бы один и тот же снимок.
        # Ждущая блокировка видна как строка `pg_locks` с `granted = false`;
        # колонки `pg_stat_activity` про ожидание тут не годятся — после
        # `SET ROLE` (shared/db.py) Postgres прячет их у чужих бэкендов, и
        # опрос по ним молча не увидел бы ничего.
        async with platform_session_scope() as session:
            blocked = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_locks lock_row "
                    "JOIN pg_stat_activity backend ON backend.pid = lock_row.pid "
                    "WHERE NOT lock_row.granted AND backend.datname = current_database()"
                )
            )
        if blocked:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_mutate_tenant_config_does_not_lose_a_concurrent_edit(
    canonical_database: None,
) -> None:
    """Две одновременные правки дают ОБЕ (§7) — на реальных сессиях, не на моке.

    Чередование задано жёстко: первая правка взяла строку и ещё не
    закоммитилась, вторая в этот момент уже упёрлась в блокировку. Дальше
    решает `FOR UPDATE`: с ним вторая, дождавшись, ПЕРЕЧИТЫВАЕТ строку и
    дописывает свой факт к обновлённому конфигу; без него она прочитала бы
    состояние до первой правки и записала бы конфиг целиком поверх — факт
    «Wi-Fi» исчез бы молча, ровно как у двух менеджеров, открывших справочник
    отеля одновременно.
    """
    tenant_id = await _tenant_with_facts("race-hotel", [_fact("Завтрак")])

    async def second_edit() -> None:
        async with platform_session_scope() as session:
            await mutate_tenant_config(session, tenant_id, _appending_fact("Парковка"))

    async with platform_session_scope() as first_session:
        await mutate_tenant_config(first_session, tenant_id, _appending_fact("Wi-Fi"))
        second = asyncio.create_task(second_edit())
        blocked = await _wait_until_a_backend_blocks()
    # Выход из scope — коммит первой правки: вторая просыпается здесь.
    await second

    assert blocked, "вторая правка не встала на блокировку — чередования не было"
    async with platform_session_scope() as session:
        loaded = await load_tenant_config(session, tenant_id)
    assert sorted(fact.topic for fact in loaded.hotel_facts) == ["Wi-Fi", "Завтрак", "Парковка"]


async def test_mutate_tenant_config_rejects_a_tenant_without_config(
    canonical_database: None,
) -> None:
    """Онбординг не завершён — правит нечего: код каталога, а не 500 (§7)."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="bare-mutate-hotel", name="Bare")
        session.add(tenant)
        await session.flush()
        with pytest.raises(AppError) as error:
            await mutate_tenant_config(session, tenant.id, _appending_fact("Wi-Fi"))
    assert error.value.code == "ERR-PLATFORM-005"
