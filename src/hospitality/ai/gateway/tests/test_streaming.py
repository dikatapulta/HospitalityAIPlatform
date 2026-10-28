"""Поток шлюза (ADR-024 §5; spec 0048 §11, PR 2) на Fake-провайдерах.

Деньги потока — в трёх исходах, которых у `complete()` нет: обрыв потребителем,
отмена задачи и поток без `usage`. Каждый обязан оставить строку журнала с
ненулевой стоимостью и снять резерв — иначе оплаченный ответ невидим бюджету.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from decimal import Decimal

import anyio
import pytest
from sqlalchemy import select

from hospitality.ai.gateway import service
from hospitality.ai.gateway.api import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PROVIDER_ERROR,
    LlmMessage,
    LlmRequest,
    LlmStreamCompleted,
    LlmTextDelta,
    MockLlmProvider,
    ToolSpec,
    stream,
)
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import LlmProviderStreamEnd, LlmProviderTimeoutError
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database", "mentor_enabled")

MENTOR_MODEL = "gpt-6-luna"
REQUEST = LlmRequest(
    messages=[LlmMessage(role="user", content="Как встречать гостя?")],
    purpose="mentor",
    max_tokens=400,
)


@pytest.fixture
def mentor_enabled() -> Iterator[None]:
    """Назначение `mentor` включено. Свой `MonkeyPatch.context()`, а не общий
    `monkeypatch`: иначе откат окружения зависел бы от порядка фикстур, и временная
    БД `canonical_database` удалялась бы раньше отката `POSTGRES_DB` (tests/conftest.py)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_MENTOR", MENTOR_MODEL)
        patch.setenv("OPENAI_API_KEY", "sk-test")
        patch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def _rows(tenant_id: uuid.UUID) -> list[LlmCallLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog).order_by(LlmCallLog.created_at)))


async def _reservations(tenant_id: uuid.UUID) -> list[LlmBudgetReservation]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmBudgetReservation)))


class _ScriptedStream:
    """Поток из заданных шагов: строка — кусок, исключение — сбой, `end` — итог."""

    name = "mock"

    def __init__(self, *attempts: list[str | Exception | LlmProviderStreamEnd]) -> None:
        self._attempts = list(attempts)
        self.calls = 0
        self.closed = False
        self.chunk_sent = asyncio.Event()

    async def stream(self, request: LlmRequest) -> AsyncGenerator[str | LlmProviderStreamEnd, None]:
        self.calls += 1
        steps = self._attempts.pop(0)
        try:
            for step in steps:
                if isinstance(step, Exception):
                    raise step
                yield step
                if isinstance(step, str):
                    self.chunk_sent.set()
            await asyncio.Event().wait()  # итога нет — висим, пока не оборвут
        finally:
            self.closed = True


def _end(
    input_tokens: int | None = 10_000, output_tokens: int | None = 400
) -> LlmProviderStreamEnd:
    return LlmProviderStreamEnd(
        model=MENTOR_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason="completed",
    )


async def test_stream_yields_chunks_then_completed_and_logs_cost(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(
        text="Улыбнитесь и поздоровайтесь.",
        model=MENTOR_MODEL,
        input_tokens=10_000,
        output_tokens=400,
    )

    with tenant_context(tenant_a):
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            received = [event async for event in events]

    deltas = [event.text for event in received if isinstance(event, LlmTextDelta)]
    assert deltas == ["Улыбнитесь ", "и ", "поздоровайтесь."]
    completed = received[-1]
    assert isinstance(completed, LlmStreamCompleted)
    assert completed.response.text == "Улыбнитесь и поздоровайтесь."
    assert completed.response.cost_usd == Decimal("0.0012")

    [row] = await _rows(tenant_a)
    assert row.id == completed.response.call_id
    assert (row.status, row.purpose, row.cost_usd) == (
        LlmCallStatus.OK,
        "mentor",
        Decimal("0.0012"),
    )
    assert await _reservations(tenant_a) == []


async def test_consumer_closing_mid_stream_logs_aborted_at_reserve_cost(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сотрудник закрыл вкладку посреди ответа — расход есть, и он виден бюджету."""
    tenant_a, _ = two_tenants
    provider = _ScriptedStream(["Улыбнитесь "])
    reserve_usd = service._reserve_usd(REQUEST)

    with tenant_context(tenant_a):
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            async for event in events:
                assert isinstance(event, LlmTextDelta)
                break  # потребитель ушёл после первого куска

    assert provider.closed  # соединение с провайдером закрыто вместе с потоком
    [row] = await _rows(tenant_a)
    assert row.status is LlmCallStatus.ABORTED
    assert row.cost_usd == reserve_usd > 0
    assert await _reservations(tenant_a) == []


async def test_cancelled_task_mid_stream_still_logs_aborted(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Отмена задачи (клиент ушёл, Starlette отменил обработчик) — та же строка `aborted`."""
    tenant_a, _ = two_tenants
    provider = _ScriptedStream(["Улыбнитесь "])

    async def consume() -> None:
        with tenant_context(tenant_a):
            async with aclosing(stream(REQUEST, provider=provider)) as events:
                async for _ in events:
                    pass

    task = asyncio.create_task(consume())
    await provider.chunk_sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await _rows(tenant_a)
    assert row.status is LlmCallStatus.ABORTED
    assert row.cost_usd == service._reserve_usd(REQUEST)
    assert await _reservations(tenant_a) == []


async def test_timeout_before_first_chunk_is_retried(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    provider = _ScriptedStream([LlmProviderTimeoutError("slow")], ["Да.", _end()])

    with tenant_context(tenant_a):
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            received = [event async for event in events]

    assert provider.calls == 2
    assert isinstance(received[-1], LlmStreamCompleted)
    assert [row.status for row in await _rows(tenant_a)] == [LlmCallStatus.OK]


async def test_failure_after_first_chunk_is_not_retried_and_costs_the_reserve(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Кусок уже ушёл человеку: повтор начал бы ответ заново — ERR-AI-003 (ADR-024 §5)."""
    tenant_a, _ = two_tenants
    provider = _ScriptedStream(["Улыбнитесь ", LlmProviderTimeoutError("stalled")], ["лишний"])

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            async for _ in events:
                pass

    assert error.value.code == ERR_AI_PROVIDER_ERROR
    assert provider.calls == 1
    [row] = await _rows(tenant_a)
    assert row.status is LlmCallStatus.ERROR
    assert row.cost_usd == service._reserve_usd(REQUEST)


async def test_stream_without_usage_costs_the_reserve_not_zero(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Нет `usage` — стоимость резерва: новый путь не наследует #426 §1."""
    tenant_a, _ = two_tenants
    provider = _ScriptedStream(["Да.", _end(input_tokens=None, output_tokens=None)])

    with tenant_context(tenant_a):
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            received = [event async for event in events]

    completed = received[-1]
    assert isinstance(completed, LlmStreamCompleted)
    reserve_usd = service._reserve_usd(REQUEST)
    assert completed.response.cost_usd == reserve_usd
    [row] = await _rows(tenant_a)
    assert (row.status, row.cost_usd) == (LlmCallStatus.OK, reserve_usd)


async def test_exhausted_purpose_budget_is_refused_before_provider(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_MENTOR_DAILY_BUDGET_USD", "0")
    get_settings.cache_clear()
    provider = _ScriptedStream(["не должен"])

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            async for _ in events:
                pass

    assert error.value.code == ERR_AI_BUDGET_EXCEEDED
    assert provider.calls == 0
    assert [row.status for row in await _rows(tenant_a)] == [LlmCallStatus.BUDGET_EXCEEDED]
    assert await _reservations(tenant_a) == []


async def test_tools_are_refused_in_stream(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    request = REQUEST.model_copy(
        update={"tools": [ToolSpec(name="x", description="x", input_schema={"type": "object"})]}
    )
    with tenant_context(tenant_a), pytest.raises(ValueError, match="инструменты"):
        async with aclosing(stream(request, provider=MockLlmProvider())) as events:
            async for _ in events:
                pass


async def test_anyio_scope_cancel_mid_stream_still_logs_aborted(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Отмена Starlette (anyio) повторяется на каждом `await` — запись идёт под щитом."""
    tenant_a, _ = two_tenants
    provider = _ScriptedStream(["Улыбнитесь "])

    with tenant_context(tenant_a), anyio.CancelScope() as scope:
        async with aclosing(stream(REQUEST, provider=provider)) as events:
            async for _ in events:
                scope.cancel()

    assert scope.cancelled_caught
    [row] = await _rows(tenant_a)
    assert row.status is LlmCallStatus.ABORTED
    assert row.cost_usd == service._reserve_usd(REQUEST)
    assert await _reservations(tenant_a) == []
