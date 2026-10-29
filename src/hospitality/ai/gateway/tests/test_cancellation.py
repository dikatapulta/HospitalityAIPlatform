"""Отмена задачи посреди `complete()` и `embed()` (#531): как у потока (ADR-024 §5).

Клиент ушёл и Starlette отменил обработчик (anyio — отмена повторяется на каждом
`await`), или задачу отменили напрямую (asyncio) — пока провайдер отвечал. Вызов
оплачен, поэтому строка `aborted` со стоимостью резерва обязана дойти до журнала,
а резерв — сняться сразу, а не висеть до конца аренды.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine, Iterator
from decimal import Decimal
from typing import Any

import anyio
import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    LlmMessage,
    LlmRequest,
    MockEmbeddingProvider,
    MockLlmProvider,
    complete,
    embed,
)
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import LlmProviderEmbeddings, LlmProviderResult
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database", "mentor_enabled")

REQUEST = LlmRequest(
    messages=[LlmMessage(role="user", content="Перескажи разговор.")],
    purpose="mentor",
    max_tokens=500,
)


@pytest.fixture
def mentor_enabled() -> Iterator[None]:
    """Канон `test_streaming.py`: свой `MonkeyPatch.context()`, а не общий `monkeypatch`."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_MENTOR", "gpt-6-luna")
        patch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


class _HangingChat(MockLlmProvider):
    """Провайдер принял запрос и думает — дольше, чем живёт задача."""

    def __init__(self) -> None:
        super().__init__()
        self.called = anyio.Event()

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.called.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


class _HangingEmbeddings(MockEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.called = anyio.Event()

    async def embed(self, texts: list[str]) -> LlmProviderEmbeddings:
        self.called.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


Call = Callable[[], Coroutine[Any, Any, object]]


def _calls() -> list[tuple[str, Callable[[], tuple[anyio.Event, Call]]]]:
    def chat() -> tuple[anyio.Event, Call]:
        provider = _HangingChat()
        return provider.called, lambda: complete(REQUEST, provider=provider)

    def embeddings() -> tuple[anyio.Event, Call]:
        provider = _HangingEmbeddings()
        return provider.called, lambda: embed(["Статья 30"], purpose="mentor", provider=provider)

    return [("complete", chat), ("embed", embeddings)]


async def _journal(tenant_id: uuid.UUID) -> tuple[list[LlmCallLog], list[LlmBudgetReservation]]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = list(await session.scalars(select(LlmCallLog)))
            reservations = list(await session.scalars(select(LlmBudgetReservation)))
    return rows, reservations


async def _reserved(tenant_id: uuid.UUID) -> Decimal:
    _, [reservation] = await _journal(tenant_id)
    return reservation.amount_usd


@pytest.mark.parametrize(("name", "build"), _calls(), ids=[name for name, _ in _calls()])
async def test_anyio_cancel_while_provider_answers_logs_aborted_and_releases(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    name: str,
    build: Callable[[], tuple[anyio.Event, Call]],
) -> None:
    """Отмена Starlette (anyio scope) — та, что приходит, когда сотрудник ушёл."""
    tenant_a, _ = two_tenants
    called, call = build()
    reserved = Decimal(-1)

    with tenant_context(tenant_a):
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            await called.wait()
            reserved = await _reserved(tenant_a)
            group.cancel_scope.cancel()

    [row], reservations = await _journal(tenant_a)
    assert row.status is LlmCallStatus.ABORTED
    assert row.cost_usd == reserved > 0
    assert reservations == []


@pytest.mark.parametrize(("name", "build"), _calls(), ids=[name for name, _ in _calls()])
async def test_task_cancel_while_provider_answers_logs_aborted_and_releases(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    name: str,
    build: Callable[[], tuple[anyio.Event, Call]],
) -> None:
    """`task.cancel()` asyncio щит anyio пропускает — запись идёт отдельной задачей."""
    tenant_a, _ = two_tenants
    called, call = build()

    async def run() -> None:
        with tenant_context(tenant_a):
            await call()

    task = asyncio.create_task(run())
    await called.wait()
    reserved = await _reserved(tenant_a)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row], reservations = await _journal(tenant_a)
    assert row.status is LlmCallStatus.ABORTED
    assert row.cost_usd == reserved > 0
    assert reservations == []
