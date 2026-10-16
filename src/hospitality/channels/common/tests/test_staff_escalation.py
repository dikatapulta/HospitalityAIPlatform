"""Модель позвала сотрудника — карточка в кабинете (spec 0040, issue #101).

Сквозной путь на настоящем оркестраторе со скриптованным провайдером: вызов
`escalate_to_staff` → эскалация `guest_requested_staff` → несрочная заявка в
службе эскалаций с сутью просьбы → реплика гостю из аргумента. Механизм
карточки целиком — spec 0039, его тесты — `test_escalation_requests.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.channels.common.guest_turn import run_guest_turn
from hospitality.channels.common.models import ConversationEscalation
from hospitality.channels.common.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis


@pytest.fixture
def requests_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Этап 3: флаг заявок включён — у модели есть `escalate_to_staff`."""
    monkeypatch.setenv("ENABLE_SERVICE_REQUESTS", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
async def hotel(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch, requests_mode: None
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Отель «только кабинет»: служба эскалаций `reception`, веб-диалог гостя."""
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        conversation_id = await ensure_conversation("web", "stay-1")
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            demo_tenant,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                escalation_category_key="reception",
            ),
        )
    yield demo_tenant, conversation_id


async def test_staff_call_opens_a_card_with_what_the_guest_needs(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """DoD #101 на отеле без бота: «да» на предложение сотрудника — карточка
    в кабинете, суть — в подробностях, строка эскалации ссылается на неё."""
    tenant_id, conversation_id = hotel
    provider = ScriptedLlmProvider(
        [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="toolu_staff",
                        name="escalate_to_staff",
                        arguments={
                            "summary": "Узнать цену позднего выезда",
                            "reply_to_guest": "Передал вашу просьбу — к вам подойдут.",
                        },
                    )
                ]
            )
        ]
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with tenant_context(tenant_id):
        await run_guest_turn(
            conversation_id,
            "да",
            uuid.uuid4(),
            external_id="stay-1",
            rate_limit_key="stay-1",
            reply=reply,
            provider=provider,
            verified_room_number="305",
        )
        (request,) = (await requests_api.list_requests(limit=5, offset=0)).items
        async with session_scope() as session:
            (row,) = (await session.scalars(select(ConversationEscalation))).all()

    assert request.summary == "Гость просит сотрудника: «да»"
    assert request.details == "Что бот понял: Узнать цену позднего выезда"
    assert request.is_urgent is False
    assert request.origin is requests_api.ServiceRequestOrigin.ESCALATION
    assert request.room_number == "305"
    assert (row.reason, row.request_id) == ("guest_requested_staff", request.id)
    assert replies == ["Передал вашу просьбу — к вам подойдут."]
