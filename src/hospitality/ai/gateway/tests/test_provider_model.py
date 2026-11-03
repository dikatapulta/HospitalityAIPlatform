"""Модель фактического провайдера на всех исходах вызова (#268).

Bake-off (§7.7) зовёт `complete(request, provider=...)` провайдером кандидата,
собранным мимо `LLM_MODEL`. Журнал и резерв обязаны видеть модель кандидата: иначе
таймауты и ошибки кандидата лягут под модель из `.env`, а резерв дорогого кандидата
занизится. Здесь `LLM_MODEL` — Sonnet 5, кандидат — Opus 4.8 Fake-адаптера.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import select

from hospitality.ai.gateway import service
from hospitality.ai.gateway.anthropic_provider import AnthropicProvider
from hospitality.ai.gateway.api import (
    LlmMessage,
    LlmProvider,
    LlmRequest,
    MockLlmProvider,
    MockTurn,
    ScriptedLlmProvider,
    complete,
)
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.provider import LlmProviderError, LlmProviderResult
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

GUEST_MODEL = "claude-sonnet-5"
CANDIDATE = "claude-opus-4-8"
REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])
GUARD_SECONDS = 10.0


@pytest.fixture(autouse=True)
def _guest_model() -> Iterator[None]:
    """`LLM_MODEL` — не кандидат. Свой `MonkeyPatch.context()` — канон test_purposes."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL", GUEST_MODEL)
        patch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def _journal(tenant_id: uuid.UUID) -> tuple[list[LlmCallLog], list[LlmBudgetReservation]]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = list(await session.scalars(select(LlmCallLog)))
            reservations = list(await session.scalars(select(LlmBudgetReservation)))
    return rows, reservations


async def test_every_adapter_declares_the_model_it_answers_with() -> None:
    """`LlmProvider.model` — та же модель, что в результате вызова (контракт порта)."""
    fakes: list[LlmProvider] = [
        MockLlmProvider(model="claude-haiku-4-5"),
        ScriptedLlmProvider([MockTurn(text="ok")], model="claude-haiku-4-5"),
    ]
    for fake in fakes:
        assert fake.model == (await fake.complete(REQUEST)).model == "claude-haiku-4-5"

    anthropic = AnthropicProvider(api_key="sk-ant-test", model=CANDIDATE, timeout_seconds=1)
    openai = OpenAIProvider(api_key="sk-test", model="gpt-6-luna", timeout_seconds=1)
    assert (anthropic.model, openai.model) == (CANDIDATE, "gpt-6-luna")


@pytest.mark.parametrize(
    ("status", "provider", "budget"),
    [
        (LlmCallStatus.TIMEOUT, lambda: MockLlmProvider(timeouts_before_success=100), "10"),
        (
            LlmCallStatus.ERROR,
            lambda: MockLlmProvider(errors_before_success=[LlmProviderError("invalid api key")]),
            "10",
        ),
        (LlmCallStatus.BUDGET_EXCEEDED, MockLlmProvider, "0"),
    ],
    ids=["timeout", "error", "budget"],
)
async def test_outcome_without_result_is_logged_under_the_provider_model(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    status: LlmCallStatus,
    provider: Callable[[], MockLlmProvider],
    budget: str,
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", budget)
    get_settings.cache_clear()

    with tenant_context(tenant_a), pytest.raises(AppError):
        await complete(REQUEST, provider=provider())

    [row], reservations = await _journal(tenant_a)
    assert (row.status, row.model) == (status, CANDIDATE)
    assert reservations == []


class _Hanging(MockLlmProvider):
    """Кандидат принял запрос и думает — дольше, чем живёт задача."""

    def __init__(self) -> None:
        super().__init__()
        self.called = asyncio.Event()

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.called.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_reserve_and_aborted_row_are_priced_by_the_provider_model(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Резерв — по цене кандидата, а не `LLM_MODEL`: Opus дороже Sonnet 5 в 2,5 раза."""
    tenant_a, _ = two_tenants
    provider = _Hanging()

    async def run() -> None:
        with tenant_context(tenant_a):
            await complete(REQUEST, provider=provider)

    task = asyncio.create_task(run())
    await asyncio.wait_for(provider.called.wait(), GUARD_SECONDS)
    _, [reservation] = await _journal(tenant_a)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    reserve_usd = service._reserve_usd(REQUEST, CANDIDATE)
    assert reservation.amount_usd == reserve_usd > service._reserve_usd(REQUEST, GUEST_MODEL)
    [row], _ = await _journal(tenant_a)
    assert (row.status, row.model, row.cost_usd) == (
        LlmCallStatus.ABORTED,
        CANDIDATE,
        reserve_usd,
    )
