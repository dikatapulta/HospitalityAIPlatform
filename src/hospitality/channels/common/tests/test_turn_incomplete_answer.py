"""Неполный ответ модели в ходе гостя (#236): деградация §7.8, а не реплика на полуслове.

Отказ модели (`refusal`) и ответ, оборванный лимитом и после повтора, — ERR-AI-007
из шлюза. Канал ведёт их тем же путём, что недоступную модель: эскалация
персоналу, гостю — честное «уже зову сотрудника».
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from hospitality.ai.gateway.api import ERR_AI_INCOMPLETE_ANSWER, MockLlmProvider
from hospitality.channels.common.guest_turn import DEGRADED_REPLY, run_guest_turn
from hospitality.channels.common.models import ConversationEscalation
from hospitality.channels.common.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.test_logging import find_record, read_log_records


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_incomplete_answer_degrades_to_staff(
    demo_tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stop_reason: str,
) -> None:
    monkeypatch.setattr(
        "hospitality.shared.ratelimit.create_redis_client", lambda: FakeRateLimitRedis()
    )
    # Отель с ботом: адресат эскалации есть, гостю честно обещают сотрудника.
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "-1001")
    get_settings.cache_clear()
    provider = MockLlmProvider(text="Утюг можно взять на ресеп", stop_reason=stop_reason)
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    configure_logging()
    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        conversation_id = await ensure_conversation("web", "stay-1")
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
    degraded = find_record(read_log_records(capsys), "guest_turn_degraded")
    assert degraded["error_code"] == ERR_AI_INCOMPLETE_ANSWER
