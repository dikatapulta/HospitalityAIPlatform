"""Блок «справочник отеля» в системном промпте (spec 0036 §4, issue #333).

Проверяется ровно то, чего не видно из схемы конфига: что уходит МОДЕЛИ и в
каком месте промпта. Место — не косметика: факты стабильны у тенанта сутками и
обязаны лежать в кэшируемом префиксе, то есть до блоков хода (#138).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.prompts import load_prompt
from hospitality.ai.tools.base import ActiveRequest
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig, load_tenant_config, store_tenant_config
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context

FACTS = [
    {"topic": "Завтрак", "answer": "с 07:00 до 10:30 на 2 этаже, входит в тариф"},
    {"topic": "Wi-Fi", "answer": "сеть Grand-Guest, пароль welcome2026"},
    {"topic": "Бассейн", "answer": "закрыт на ремонт", "valid_until": "2026-08-15"},
]


async def _configure(tenant_id: uuid.UUID, facts: list[dict[str, Any]]) -> None:
    """Записать тенанту конфиг с заданным справочником (канон записи, P-12)."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig.model_validate(
                {
                    "schema_version": 1,
                    "profile": {"city": "Almaty", "country_code": "KZ"},
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "hotel_facts": facts,
                }
            ),
        )


async def _system_prompt(tenant_id: uuid.UUID, **kwargs: Any) -> str:
    """Системный промпт, который оркестратор собрал на одном ходу гостя."""
    provider = MockLlmProvider(text="ответ")
    with tenant_context(tenant_id):
        await orchestrator.handle_message(
            message="во сколько завтрак?", provider=provider, **kwargs
        )
    return provider.calls[0].system or ""


@pytest.fixture
def _today_in_almaty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заморозить «сейчас» на 15.08.2026, чтобы факт «до 15.08» был живым.

    Часы двигаются у `utc_now` — канонического «сейчас» (shared/db.py): его
    зовёт `active_hotel_facts`, и подмена именно там оставляет вычисление
    границы по поясу отеля настоящим, а не тестовым.
    """
    monkeypatch.setattr(
        "hospitality.platform.config.utc_now",
        lambda: datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
    )


async def test_facts_are_rendered_in_storage_order(
    demo_tenant: uuid.UUID, _today_in_almaty: None
) -> None:
    """Порядок строк блока — порядок хранения: его расставил отель (§4)."""
    await _configure(demo_tenant, FACTS)

    system = await _system_prompt(demo_tenant)

    assert "# Hotel facts" in system
    block = system.split("# Hotel facts", 1)[1]
    assert block.index("- Завтрак:") < block.index("- Wi-Fi:") < block.index("- Бассейн")
    assert "- Wi-Fi: сеть Grand-Guest, пароль welcome2026" in block


async def test_temporary_fact_carries_its_date(
    demo_tenant: uuid.UUID, _today_in_almaty: None
) -> None:
    """Пометка временного факта — та самая форма, которую опознаёт промпт v5."""
    await _configure(demo_tenant, FACTS)

    system = await _system_prompt(demo_tenant)

    assert "- Бассейн (temporary, until 2026-08-15): закрыт на ремонт" in system


async def test_expired_fact_is_not_rendered_but_last_day_is(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Последний день действия — включительно, следующий — уже нет (§4).

    Обе даты берутся по поясу отеля: 20:00 UTC 15 августа — это уже 16 августа
    в Алматы, и по UTC факт прожил бы у гостя лишние пять часов.
    """
    await _configure(demo_tenant, FACTS)

    monkeypatch.setattr(
        "hospitality.platform.config.utc_now",
        lambda: datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
    )
    assert "Бассейн" in await _system_prompt(demo_tenant)

    monkeypatch.setattr(
        "hospitality.platform.config.utc_now",
        lambda: datetime(2026, 8, 15, 20, 0, tzinfo=UTC),
    )
    last_day_passed = await _system_prompt(demo_tenant)
    assert "Бассейн" not in last_day_passed
    # Блок при этом остаётся: постоянные факты никуда не делись.
    assert "# Hotel facts" in last_day_passed


async def test_empty_directory_means_no_block_at_all(demo_tenant: uuid.UUID) -> None:
    """Отель с пустым справочником ведёт себя ровно как до spec 0036 (DoD #333).

    Блока нет вовсе — как у пустого списка заявок: промпт v5 велит не выдумывать
    факты, если блока не было.
    """
    await _configure(demo_tenant, [])

    system = await _system_prompt(demo_tenant)

    # Промпт равен файлу v5 буква в букву: ни одного дописанного блока.
    assert system == load_prompt(orchestrator.PROMPT_NAME)


async def test_facts_block_precedes_the_blocks_of_this_turn(
    demo_tenant: uuid.UUID, _today_in_almaty: None
) -> None:
    """Справочник стоит до блоков комнаты и заявок — защита кэшируемого префикса.

    У Anthropic кэшируется префикс до отметки `cache_control` (#138): факты
    стабильны сутками, комната и заявки меняются каждый ход. Перепутанный
    порядок промахивался бы мимо кэша каждый ход, а запись кэша стоит 1,25×
    входа — включение #138 дало бы отрицательный эффект.
    """
    await _configure(demo_tenant, FACTS)
    with tenant_context(demo_tenant):
        categories = await requests_api.list_categories()
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=next(c.id for c in categories if c.key == "housekeeping"),
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary="полотенца в 305",
            )
        )

    system = await _system_prompt(
        demo_tenant,
        verified_room_number="305",
        active_requests=[
            ActiveRequest(id=request.id, status=request.status, summary=request.summary)
        ],
    )

    assert (
        system.index("# Hotel facts")
        < system.index("# Guest's verified room")
        < system.index("# Active service requests in this conversation")
    )


async def test_tenant_without_config_keeps_working(demo_tenant: uuid.UUID) -> None:
    """Онбординг не завершён — ход идёт без справочника, а не падает (деградация).

    Конфига у тенанта нет вовсе: это состояние живого отеля между созданием и
    онбордингом, и диалог гостя в нём ценнее справочника.
    """
    system = await _system_prompt(demo_tenant)

    assert "# Hotel facts" not in system
    assert system.startswith("Reply in the exact same language")


async def test_one_config_read_per_turn_feeds_both_the_prompt_and_the_tool(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, _today_in_almaty: None
) -> None:
    """Конфиг читается РОВНО один раз за ход и кормит обоих потребителей (§4, п. 5).

    До spec 0036 конфиг читал реестр инструментов ради подсказок служб (#123).
    Появление второго потребителя — справочника отеля — дало бы второй такой же
    запрос к БД на каждую реплику гостя, поэтому чтение переехало в оркестратор.
    Тест стережёт обе половины разом: счётчик чтений и то, что подсказка службы
    после переезда по-прежнему доезжает до описания инструмента.
    """
    await _configure(demo_tenant, FACTS)
    async with platform_session_scope() as session:
        config = TenantConfig.model_validate(
            {
                "schema_version": 1,
                "profile": {"city": "Almaty", "country_code": "KZ"},
                "timezone": "Asia/Almaty",
                "default_language": "ru",
                "hotel_facts": FACTS,
                "category_hints": {"housekeeping": "кофе в пакетиках, вода"},
            }
        )
        await store_tenant_config(session, demo_tenant, config)

    reads = 0

    async def counting_load(*args: Any, **kwargs: Any) -> TenantConfig:
        nonlocal reads
        reads += 1
        return await load_tenant_config(*args, **kwargs)

    # Подменяется имя, которым конфиг читает ИМЕННО оркестратор: так счётчик
    # ловит и второе чтение, если оно вернётся в реестр инструментов.
    monkeypatch.setattr("hospitality.ai.orchestrator.load_tenant_config", counting_load)

    provider = MockLlmProvider(text="ответ")
    with tenant_context(demo_tenant):
        await orchestrator.handle_message(message="во сколько завтрак?", provider=provider)

    assert reads == 1
    call = provider.calls[0]
    assert "- Завтрак: с 07:00 до 10:30 на 2 этаже, входит в тариф" in (call.system or "")
    categories = call.tools[0].input_schema["properties"]["category_key"]["description"]
    assert "- housekeeping: кофе в пакетиках, вода" in categories


async def test_language_reminder_closes_the_prompt_and_only_with_facts(
    demo_tenant: uuid.UUID, _today_in_almaty: None
) -> None:
    """Напоминание о языке гостя стоит ПОСЛЕДНИМ и только при непустом справочнике.

    Позиция — предмет теста, а не оформление: справочник написан на языке отеля
    и лежит между правилом языка (первая строка файла промпта) и репликой
    гостя. Замер 07.09.2026 на Sonnet 5: англоязычный гость получал ответ
    по-русски, пока правило оставалось только в начале. Пустой справочник
    напоминания не получает — промпт такого отеля обязан остаться прежним.
    """
    await _configure(demo_tenant, FACTS)
    system = await _system_prompt(demo_tenant, verified_room_number="305")

    assert system.rstrip().endswith("translate everything around them.")
    assert system.index("# Before you reply") > system.index("# Guest's verified room")

    await _configure(demo_tenant, [])
    assert "# Before you reply" not in await _system_prompt(demo_tenant)
