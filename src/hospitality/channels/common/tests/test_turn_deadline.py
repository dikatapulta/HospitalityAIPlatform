"""Потолок ожидания хода гостя (issue #271, spec 0054).

Провайдер модели завис: гость получает реплику деградации §7.8, а персонал —
эскалацию, не позже потолка хода. Без потолка — после всех попыток шлюза
(3 × 30 с плюс паузы, около полутора минут).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from hospitality.ai.gateway.api import LlmRequest
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.channels.common import guest_turn
from hospitality.channels.common.guest_turn import DEGRADED_REPLY, run_guest_turn
from hospitality.channels.common.models import ConversationEscalation
from hospitality.channels.common.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

# Тест обязан упасть быстро, а не висеть полторы минуты, если потолок сломан.
GUARD_SECONDS = 10.0


class _HangingProvider:
    """Провайдер, который не отвечает никогда, — «завис» без таймаута SDK."""

    name = "mock"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.call_count += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_hanging_model_degrades_within_the_turn_deadline(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "hospitality.shared.ratelimit.create_redis_client", lambda: FakeRateLimitRedis()
    )
    # Отель с ботом: адресат эскалации есть, гостю честно обещают сотрудника.
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "-1001")
    get_settings.cache_clear()
    assert get_settings().llm_timeout_seconds >= GUARD_SECONDS  # иначе тест ничего не докажет
    monkeypatch.setattr(guest_turn, "GUEST_TURN_DEADLINE_SECONDS", 0.2)
    provider = _HangingProvider()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        conversation_id = await ensure_conversation("web", "stay-1")
        async with asyncio.timeout(GUARD_SECONDS):
            await run_guest_turn(
                conversation_id,
                "есть ли утюг?",
                uuid.uuid4(),
                external_id="stay-1",
                rate_limit_key="stay-1",
                reply=reply,
                provider=provider,
                verified_room_number="305",
            )
        async with session_scope() as session:
            escalations = await session.scalar(
                select(func.count()).select_from(ConversationEscalation)
            )

    assert replies == [DEGRADED_REPLY]
    assert escalations == 1
    assert provider.call_count == 1
