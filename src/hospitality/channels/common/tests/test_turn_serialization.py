"""Ходы одного диалога — по очереди (issue #91).

Гость шлёт два сообщения за секунду: Telegram доставляет их параллельно, веб-чат
не запирает строку ввода. Без замка второй ход грузит историю до ответа
первому, перетирает его ожидание подтверждения (P-9), и реплики уходят
вперемешку. Чередование тест задаёт сам: первый ход стоит в модели, пока
второй не упрётся в замок, — иначе тест зеленел бы на удачном расписании.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

import pytest

from hospitality.ai.gateway.api import LlmRequest
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.common.guest_turn import run_guest_turn
from hospitality.channels.common.store import (
    ensure_conversation,
    insert_inbound_message,
    record_outbound_message,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

GUARD_SECONDS = 10.0
FIRST_ANSWER = "Полотенца принесут."
SECOND_ANSWER = "И подушку тоже."


class _GatedProvider:
    """Первый вызов ждёт `release_first`, остальные отвечают сразу."""

    name = "mock"
    model = "claude-opus-4-8"

    def __init__(self) -> None:
        self.calls: list[LlmRequest] = []
        self.release_first = asyncio.Event()

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.calls.append(request)
        if len(self.calls) == 1:
            await self.release_first.wait()
            text = FIRST_ANSWER
        else:
            text = SECOND_ANSWER
        return LlmProviderResult(
            text=text,
            model="claude-opus-4-8",
            input_tokens=10,
            output_tokens=5,
            tool_calls=[],
            stop_reason="end_turn",
        )


async def _wait_for(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(GUARD_SECONDS):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRateLimitRedis:
    """Один Redis на оба хода — как в проде, где замок общий для процессов."""
    client = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: client)
    # Отель с ботом: у эскалации ЧП есть адресат (spec 0051).
    monkeypatch.setenv("TELEGRAM_STAFF_CHAT_ID", "-1001")
    get_settings.cache_clear()
    return client


async def test_second_message_waits_for_the_first_turn(
    demo_tenant: uuid.UUID, fake_redis: FakeRateLimitRedis
) -> None:
    provider = _GatedProvider()
    replies: list[str] = []

    with tenant_context(demo_tenant):
        conversation_id = await ensure_conversation("web", "stay-1")

        async def guest_says(text: str) -> None:
            inbound_id = await insert_inbound_message(
                conversation_id,
                NormalizedMessage(
                    channel="web",
                    chat_id="stay-1",
                    external_message_id=str(uuid.uuid4()),
                    idempotency_key=f"web:msg:{uuid.uuid4()}",
                    kind=MessageKind.TEXT,
                    text=text,
                ),
                "corr",
            )
            assert inbound_id is not None

            async def reply(reply_text: str) -> None:
                await record_outbound_message(
                    conversation_id, reply_text, "corr", external_message_id=None
                )
                replies.append(reply_text)

            await run_guest_turn(
                conversation_id,
                text,
                inbound_id,
                external_id="stay-1",
                rate_limit_key="stay-1",
                reply=reply,
                provider=provider,
            )

        first = asyncio.create_task(guest_says("принесите полотенца"))
        await _wait_for(lambda: len(provider.calls) == 1)
        second = asyncio.create_task(guest_says("и подушку"))
        # Второй ход либо упёрся в замок, либо (без замка) уже у модели.
        await _wait_for(lambda: fake_redis.lock_rejections > 0 or len(provider.calls) == 2)
        provider.release_first.set()
        async with asyncio.timeout(GUARD_SECONDS):
            await asyncio.gather(first, second)

    assert replies == [FIRST_ANSWER, SECOND_ANSWER]
    second_history = [(m.role, m.content) for m in provider.calls[1].messages]
    assert ("assistant", FIRST_ANSWER) in second_history  # второй ход видел ответ первому
    assert fake_redis.locks == {}


async def test_emergency_does_not_wait_for_a_running_turn(
    demo_tenant: uuid.UUID, fake_redis: FakeRateLimitRedis
) -> None:
    """ЧП-перехват (spec 0034) стоит до замка: «пожар» не ждёт ответа модели."""
    provider = _GatedProvider()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        conversation_id = await ensure_conversation("web", "stay-1")
        running = asyncio.create_task(
            run_guest_turn(
                conversation_id,
                "принесите полотенца",
                uuid.uuid4(),
                external_id="stay-1",
                rate_limit_key="stay-1",
                reply=reply,
                provider=provider,
            )
        )
        await _wait_for(lambda: len(provider.calls) == 1)
        async with asyncio.timeout(GUARD_SECONDS):
            await run_guest_turn(
                conversation_id,
                "пожар в коридоре",
                uuid.uuid4(),
                external_id="stay-1",
                rate_limit_key="stay-1",
                reply=reply,
                provider=provider,
            )
        assert len(replies) == 1 and replies[0] != FIRST_ANSWER  # ответ ЧП — раньше модели
        assert fake_redis.lock_rejections == 0
        provider.release_first.set()
        async with asyncio.timeout(GUARD_SECONDS):
            await running

    assert replies[1] == FIRST_ANSWER
