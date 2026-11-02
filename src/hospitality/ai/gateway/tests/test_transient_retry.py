"""Повтор временных отказов провайдера (issue #409, аудит тихих отказов Т-6).

До #409 шлюз повторял только таймаут: 529, 500, 429 с `retry-after` и обрыв
соединения сразу давали гостю «уже зову сотрудника», хотя повтор через секунду
прошёл бы. Постоянные отказы — ключ, баланс, 429 без `retry-after` (месячный
потолок тарифа) — по-прежнему не повторяются: повтор их не исправит.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from hospitality.ai.gateway import retry, service
from hospitality.ai.gateway.api import (
    ERR_AI_PROVIDER_ERROR,
    Deadline,
    LlmMessage,
    LlmRequest,
    MockEmbeddingProvider,
    MockLlmProvider,
    complete,
    embed,
)
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import (
    LlmProviderError,
    LlmProviderTransientError,
    retry_after_seconds,
    status_error,
)
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="есть ли утюг?")])


class _SleepSpy:
    """Подмена модуля `asyncio` в service.py: пауза записывается, но не ждётся."""

    CancelledError = asyncio.CancelledError
    timeout = staticmethod(asyncio.timeout)  # потолок вызывающего (spec 0055)

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleep_spy(monkeypatch: pytest.MonkeyPatch) -> _SleepSpy:
    spy = _SleepSpy()
    monkeypatch.setattr(service, "asyncio", spy)
    return spy


def _overloaded(retry_after: float | None = None) -> LlmProviderTransientError:
    return LlmProviderTransientError(
        "Error code: 529 - overloaded_error", kind="overloaded", retry_after_seconds=retry_after
    )


async def _statuses(tenant_id: uuid.UUID) -> list[LlmCallStatus]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog.status)))


@pytest.mark.parametrize(
    "error",
    [
        LlmProviderTransientError("connection reset", kind="connection"),
        LlmProviderTransientError("Error code: 500 - api_error", kind="server"),
        LlmProviderTransientError("Error code: 529", kind="overloaded"),
        LlmProviderTransientError("Error code: 429", kind="rate_limit", retry_after_seconds=1.0),
    ],
    ids=["connection", "500", "529", "429-retry-after"],
)
async def test_transient_error_is_retried_and_only_the_success_is_logged(
    two_tenants: tuple[uuid.UUID, uuid.UUID], sleep_spy: _SleepSpy, error: LlmProviderError
) -> None:
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(errors_before_success=[error])

    with tenant_context(tenant_a):
        response = await complete(REQUEST, provider=provider)

    assert response.text == "mock response"
    assert len(provider.calls) == 2
    assert len(sleep_spy.delays) == 1
    assert await _statuses(tenant_a) == [LlmCallStatus.OK]


async def test_exhausted_transient_errors_give_err_ai_003_and_one_error_row(
    two_tenants: tuple[uuid.UUID, uuid.UUID], sleep_spy: _SleepSpy
) -> None:
    """Попытки общие с таймаутом; после последней — ни паузы, ни повтора."""
    tenant_a, _ = two_tenants
    attempts = get_settings().llm_max_attempts
    provider = MockLlmProvider(errors_before_success=[_overloaded() for _ in range(attempts)])

    with tenant_context(tenant_a), pytest.raises(AppError) as raised:
        await complete(REQUEST, provider=provider)

    assert (raised.value.code, raised.value.status_code) == (ERR_AI_PROVIDER_ERROR, 502)
    assert len(provider.calls) == attempts
    assert len(sleep_spy.delays) == attempts - 1
    assert await _statuses(tenant_a) == [LlmCallStatus.ERROR]


@pytest.mark.parametrize(
    ("status_code", "headers"),
    [(429, {}), (401, {}), (402, {}), (400, {"retry-after": "1"})],
    ids=["429-без-retry-after", "401", "402", "400"],
)
async def test_permanent_error_is_not_retried(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    sleep_spy: _SleepSpy,
    status_code: int,
    headers: dict[str, str],
) -> None:
    """429 без `retry-after` — потолок тарифа до 1-го числа; ключ и баланс — тоже не пройдут."""
    tenant_a, _ = two_tenants
    error = status_error(f"Error code: {status_code}", status_code, headers)
    provider = MockLlmProvider(errors_before_success=[error])

    with tenant_context(tenant_a), pytest.raises(AppError) as raised:
        await complete(REQUEST, provider=provider)

    assert raised.value.code == ERR_AI_PROVIDER_ERROR
    assert len(provider.calls) == 1
    assert sleep_spy.delays == []
    assert await _statuses(tenant_a) == [LlmCallStatus.ERROR]


async def test_retry_after_is_the_floor_of_the_pause(
    two_tenants: tuple[uuid.UUID, uuid.UUID], sleep_spy: _SleepSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Раньше `retry-after` провайдер не примет — расчётная пауза короче, ждём его."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0.01")
    get_settings.cache_clear()
    provider = MockLlmProvider(errors_before_success=[_overloaded(retry_after=4.0)])

    with tenant_context(tenant_a):
        await complete(REQUEST, provider=provider)

    assert sleep_spy.delays == [4.0]


async def test_retry_after_longer_than_one_attempt_is_not_waited(
    two_tenants: tuple[uuid.UUID, uuid.UUID], sleep_spy: _SleepSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Потолок паузы — одна попытка (ADR-017): на нём держится срок аренды резерва."""
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    get_settings.cache_clear()
    provider = MockLlmProvider(errors_before_success=[_overloaded(retry_after=31.0)])

    with tenant_context(tenant_a), pytest.raises(AppError) as raised:
        await complete(REQUEST, provider=provider)

    assert raised.value.code == ERR_AI_PROVIDER_ERROR
    assert len(provider.calls) == 1
    assert sleep_spy.delays == []


async def test_pause_past_the_guest_deadline_is_not_taken(
    two_tenants: tuple[uuid.UUID, uuid.UUID], sleep_spy: _SleepSpy
) -> None:
    """Гость повтора не дождётся (spec 0055) — отказ сразу, без сна до потолка."""
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(errors_before_success=[_overloaded(retry_after=5.0)])

    with tenant_context(tenant_a), pytest.raises(AppError) as raised:
        await complete(REQUEST, provider=provider, deadline=Deadline.after(2.0))

    assert raised.value.code == ERR_AI_PROVIDER_ERROR
    assert len(provider.calls) == 1
    assert sleep_spy.delays == []


async def test_embed_retries_transient_error(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Эмбеддинги — та же политика повторов, что у `complete()` (`retry.py`)."""
    tenant_a, _ = two_tenants
    monkeypatch.setattr(retry, "_retry_pause_seconds", lambda attempt, settings: 0.0)
    provider = MockEmbeddingProvider(errors_before_success=[_overloaded()])

    with tenant_context(tenant_a):
        response = await embed(["Статья 30"], purpose="guest", provider=provider)

    assert len(response.vectors) == 1
    assert len(provider.calls) == 2
    assert await _statuses(tenant_a) == [LlmCallStatus.OK]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "2"}, 2.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
        ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"retry-after": "-1"}, None),
        ({}, None),
    ],
)
def test_retry_after_header_is_read_in_seconds(
    headers: dict[str, str], expected: float | None
) -> None:
    assert retry_after_seconds(headers) == expected


def test_lease_outlasts_every_attempt_and_every_retry_after_pause() -> None:
    """Пауза по `retry-after` бывает длиной в попытку — резерв не истекает посреди вызова."""
    settings = Settings(
        llm_timeout_seconds=30.0, llm_max_attempts=3, llm_retry_backoff_base_seconds=1.0
    )
    worst_call = 3 * 30.0 + 2 * 30.0  # три попытки и две паузы по потолку

    assert service._reservation_lease_seconds(settings) > worst_call
