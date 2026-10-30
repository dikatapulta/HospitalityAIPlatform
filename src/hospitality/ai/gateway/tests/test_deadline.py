"""Потолок ожидания вызова шлюза (issue #271, spec 0055).

Без потолка худший вызов — `LLM_MAX_ATTEMPTS × LLM_TIMEOUT_SECONDS` плюс
паузы: при умолчаниях около 93 секунд. С потолком попытка обрывается в момент
потолка, а новая не начинается, если гость её уже не дождётся. Исход — тот же
ERR-AI-001 и строка `timeout` в журнале, что у исчерпанных попыток.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    ERR_AI_PROVIDER_TIMEOUT,
    Deadline,
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    complete,
)
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")

REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="есть ли утюг?")])
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


async def _statuses(tenant_id: uuid.UUID) -> list[LlmCallStatus]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(select(LlmCallLog.status))
            return list(rows)


async def test_hanging_provider_is_cut_at_the_deadline(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Таймаут SDK — 30 с, попыток три, а гость ждёт не дольше потолка."""
    tenant_a, _ = two_tenants
    assert get_settings().llm_timeout_seconds >= GUARD_SECONDS  # иначе тест ничего не докажет
    provider = _HangingProvider()

    started = time.monotonic()
    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        async with asyncio.timeout(GUARD_SECONDS):
            await complete(REQUEST, provider=provider, deadline=Deadline.after(0.2))

    assert error.value.code == ERR_AI_PROVIDER_TIMEOUT
    assert time.monotonic() - started < GUARD_SECONDS / 2
    # Оборванная попытка съела весь потолок — второй не было.
    assert provider.call_count == 1
    assert await _statuses(tenant_a) == [LlmCallStatus.TIMEOUT]


async def test_no_retry_when_the_pause_outlasts_the_deadline(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Быстрый таймаут, но пауза перед повтором (5–10 с) длиннее остатка
    потолка: шлюз сдаётся сразу, а не спит «на дорожку» и не стреляет второй раз."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "10.0")
    get_settings.cache_clear()
    provider = MockLlmProvider(timeouts_before_success=1)

    started = time.monotonic()
    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(REQUEST, provider=provider, deadline=Deadline.after(1.0))

    assert error.value.code == ERR_AI_PROVIDER_TIMEOUT
    assert time.monotonic() - started < 4.0  # без паузы в 5–10 с
    assert len(provider.calls) == 1
    assert await _statuses(tenant_a) == [LlmCallStatus.TIMEOUT]


async def test_retry_still_happens_inside_the_deadline(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Времени хватает — потолок повтор не отнимает: таймаут → пауза → ответ."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0.01")
    get_settings.cache_clear()
    provider = MockLlmProvider(timeouts_before_success=1)

    with tenant_context(tenant_a):
        response = await complete(REQUEST, provider=provider, deadline=Deadline.after(25.0))

    assert response.text == "mock response"
    assert len(provider.calls) == 2
    assert await _statuses(tenant_a) == [LlmCallStatus.OK]


async def test_spent_deadline_does_not_reach_the_provider(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Второй вызов хода, которому от общего потолка ничего не осталось:
    провайдеру не уходит ни одного запроса, исход всё равно в журнале."""
    tenant_a, _ = two_tenants
    provider = MockLlmProvider()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(REQUEST, provider=provider, deadline=Deadline.after(-1.0))

    assert error.value.code == ERR_AI_PROVIDER_TIMEOUT
    assert provider.calls == []
    with tenant_context(tenant_a):
        async with session_scope() as session:
            row = await session.scalar(select(LlmCallLog))
    assert row is not None
    assert row.status is LlmCallStatus.TIMEOUT
    assert row.cost_usd == Decimal(0)
