"""Блок «Staying longer» в системном промпте хода гостя (spec 0059 §2.4, §5 п. 4).

Outlook настоящий — `get_extension_outlook` по Fake-адаптеру; тест на верхнем
уровне, как `test_stay_extension_outlook.py`: адаптеры из `ai/` не импортируются
(контракт 2 import-linter). Текст блока по готовому outlook —
`ai/tests/test_stay_extension.py`. F811 отключён на файл: параметр-фикстура обязан
называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import timedelta

from hospitality.ai import orchestrator
from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider
from hospitality.ai.tools.base import ActiveRequest
from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelFact, load_tenant_config, store_tenant_config
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from tests.test_pms_sync import (  # noqa: F401  (фикстуры для pytest)
    add_hotel,
    hotel,
    pms,
    today,
)
from tests.test_stay_extension_outlook import free_rooms, stay_until


async def _system_prompt(
    tenant_id: uuid.UUID,
    *,
    stay_id: uuid.UUID | None,
    active_requests: tuple[ActiveRequest, ...] = (),
) -> str:
    provider = ScriptedLlmProvider([MockTurn(text="Здравствуйте!")])
    with tenant_context(tenant_id):
        await orchestrator.handle_message(
            message="Можно остаться ещё на две ночи?",
            provider=provider,
            active_requests=active_requests,
            verified_room_number="305" if stay_id is not None else None,
            verified_stay_id=stay_id,
        )
    return provider.calls[0].system or ""


async def _with_fact(tenant_id: uuid.UUID) -> None:
    """Справочник отеля — без него нет и правила языка последней строкой (spec 0036 §5)."""
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
        fact = HotelFact(topic="Завтрак", answer="с 07:00 до 10:30")
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"hotel_facts": [fact]})
        )


async def test_block_sits_after_room_and_before_requests_and_language(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    departure = today() + timedelta(days=2)
    stay_id = await stay_until(hotel, departure)
    await _with_fact(hotel)
    pms.set_availability(hotel, free_rooms(today(), 5, 5, 5, 0, 5), taken_at=utc_now())
    request = ActiveRequest(
        id=uuid.uuid4(), status=requests_api.RequestStatus.NEW, summary="полотенца"
    )

    system = await _system_prompt(hotel, stay_id=stay_id, active_requests=(request,))

    order = [
        system.index("# Guest's verified room"),
        system.index("# Staying longer"),
        system.index("# Active service requests in this conversation"),
        system.index("# Before you reply"),
    ]
    assert order == sorted(order)
    full_night = departure + timedelta(days=1)
    assert f"Nights with NO free rooms: {full_night.day}–" in system


async def test_no_block_without_stay_binding(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Гость без привязки к Stay (Telegram, #484) — блока нет, бот как v1."""
    pms.set_availability(hotel, free_rooms(today(), 5, 5, 5), taken_at=utc_now())

    assert "# Staying longer" not in await _system_prompt(hotel, stay_id=None)


async def test_no_block_at_hotel_without_opera(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Выключатель — `pms_source: opera_report` (§2.5): у ручного отеля файл не читается."""
    manual = await add_hotel("hotel-manual", pms_source="manual")
    stay_id = await stay_until(manual, today() + timedelta(days=2))
    pms.set_availability(manual, free_rooms(today(), 5, 5, 5, 5), taken_at=utc_now())

    assert "# Staying longer" not in await _system_prompt(manual, stay_id=stay_id)
