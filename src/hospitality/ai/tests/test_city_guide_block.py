"""Блок «City guide» в системном промпте гостя (spec 0085 §4, §5, #952).

Проверяется то, что уходит МОДЕЛИ: есть ли блок, где он стоит и что в нём, — и
что отель без флага получает промпт прежним байт в байт.
"""

from __future__ import annotations

import uuid
from typing import Any

from structlog.testing import capture_logs

from hospitality.ai import orchestrator
from hospitality.ai.city_guide import render_city_guide_block
from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.prompts import load_prompt
from hospitality.modules.concierge import api as concierge_api
from hospitality.platform.config import TenantConfig, store_tenant_config
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context


async def _configure(tenant_id: uuid.UUID, *, city: str = "Almaty", **fields: Any) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig.model_validate(
                {
                    "schema_version": 1,
                    "profile": {"city": city, "country_code": "KZ"},
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    **fields,
                }
            ),
        )


async def _system_prompt(tenant_id: uuid.UUID) -> str:
    provider = MockLlmProvider(text="ответ")
    with tenant_context(tenant_id):
        await orchestrator.handle_message(message="как добраться до Медеу?", provider=provider)
    return provider.calls[0].system or ""


def _almaty() -> concierge_api.CityGuide:
    guide = concierge_api.city_guide_for("Almaty", "KZ")
    assert guide is not None
    return guide


async def test_flag_off_keeps_the_prompt_byte_for_byte(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Флаг по умолчанию выключен: отель без него получает файл промпта как есть (§5)."""
    await _configure(demo_tenant)

    assert await _system_prompt(demo_tenant) == load_prompt(orchestrator.PROMPT_NAME)


async def test_flag_on_puts_the_guide_after_hotel_facts_and_before_the_turn(
    demo_tenant: uuid.UUID,
) -> None:
    """Справочник — в кэшируемом префиксе: после фактов отеля, до блоков хода (§4, #138)."""
    facts = [{"topic": "Wi-Fi", "answer": "сеть Guest, пароль 123"}]
    await _configure(demo_tenant, city_guide_enabled=True, hotel_facts=facts)

    with capture_logs() as logs:
        system = await _system_prompt(demo_tenant)

    assert system.index("# Hotel facts") < system.index("# City guide: Almaty")
    assert system.index("# City guide: Almaty") < system.index("# Service requests are switched")
    assert render_city_guide_block(_almaty()) in system
    event = next(log for log in logs if log["event"] == "city_guide_in_context")
    assert (event["city"], event["entries"]) == ("almaty", len(_almaty().entries))


async def test_flag_on_for_a_city_without_a_guide_adds_nothing_and_warns(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Отель в Астане справочника Алматы не получает; включивший видит warning (§5)."""
    await _configure(demo_tenant, city="Astana", city_guide_enabled=True)

    with capture_logs() as logs:
        system = await _system_prompt(demo_tenant)

    assert system == load_prompt(orchestrator.PROMPT_NAME)
    warning = next(log for log in logs if log["event"] == "city_guide_not_found")
    assert (warning["log_level"], warning["city"]) == ("warning", "Astana")


def test_block_lines_carry_local_name_and_map_link_built_by_code() -> None:
    """Строка записи: тема, местное имя в скобках, текст, ссылка карты от кода (§4)."""
    guide = concierge_api.CityGuide.model_validate(
        {
            "key": "testcity",
            "name": "Almaty",
            "country_code": "KZ",
            "aliases": ["Almaty"],
            "entries": [
                {
                    "topic": "Green Bazaar",
                    "text": "The city's best-known covered market.",
                    "local_name": "Зелёный базар",
                    "map": "Green Bazaar",
                    "source": "https://example.org/bazaar",
                    "checked": "2026-11-11",
                },
                {
                    "topic": "Currency",
                    "text": "The currency is the tenge (KZT).",
                    "source": "https://example.org/tenge",
                    "checked": "2026-11-11",
                },
            ],
        }
    )

    block = render_city_guide_block(guide)

    assert (
        "- Green Bazaar (Зелёный базар): The city's best-known covered market. "
        "Map: https://www.google.com/maps/search/?api=1&query=Green+Bazaar+Almaty"
    ) in block.splitlines()
    assert "- Currency: The currency is the tenge (KZT)." in block.splitlines()
    assert "`report_unanswered_question`" in block
