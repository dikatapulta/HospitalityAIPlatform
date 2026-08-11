"""Тесты AI Gateway на mock-провайдере (Task 0014, R-7).

Покрывают DoD задачи: каждый вызов виден в БД (стоимость, хэш промпта,
correlation id, latency), ретрай при таймауте, простейший бюджетный лимит
тенанта (превышение → отказ до обращения к провайдеру, соседний тенант
не страдает — RLS).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import structlog
from prometheus_client import generate_latest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PROVIDER_TIMEOUT,
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    complete,
    compute_prompt_hash,
    refresh_budget_metrics,
)
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import LlmProviderError, LlmProviderResult
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.metrics import set_llm_daily_budget
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")

SIMPLE_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])


def _gauge(name: str, tenant_id: uuid.UUID) -> float | None:
    """Значение gauge с лейблом тенанта из выдачи /metrics."""
    prefix = f'{name}{{tenant_id="{tenant_id}"}} '
    for line in generate_latest().decode().splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    return None


async def _call_log_rows(tenant_id: uuid.UUID) -> list[LlmCallLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(select(LlmCallLog).order_by(LlmCallLog.created_at))
            return list(rows)


async def test_call_is_logged_with_cost_prompt_hash_and_correlation_id(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    # 200K input × $5/MTok + 40K output × $25/MTok = $1 + $1 = $2.
    provider = MockLlmProvider(text="Здравствуйте!", input_tokens=200_000, output_tokens=40_000)
    structlog.contextvars.bind_contextvars(correlation_id="test-correlation-id")

    with tenant_context(tenant_a):
        response = await complete(SIMPLE_REQUEST, provider=provider)

    assert response.text == "Здравствуйте!"
    assert response.cost_usd == Decimal("2")
    assert response.prompt_hash == compute_prompt_hash(SIMPLE_REQUEST)

    rows = await _call_log_rows(tenant_a)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == response.call_id
    assert row.status is LlmCallStatus.OK
    assert row.provider == "mock"
    assert row.model == "claude-opus-4-8"
    assert row.prompt_hash == response.prompt_hash
    assert row.correlation_id == "test-correlation-id"
    assert row.input_tokens == 200_000
    assert row.output_tokens == 40_000
    assert row.cost_usd == Decimal("2")
    assert row.latency_ms >= 0


async def test_retry_after_timeout_succeeds(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    # Пауза между попытками (issue #46) здесь живая, но укороченная: её
    # величину проверяет test_budget_reservation.py, а тут она только
    # растянула бы прогон на секунды.
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0.01")
    get_settings.cache_clear()
    provider = MockLlmProvider(timeouts_before_success=1)

    with tenant_context(tenant_a):
        response = await complete(SIMPLE_REQUEST, provider=provider)

    # Первая попытка — таймаут, вторая — успех; в журнале один успешный вызов.
    assert len(provider.calls) == 2
    assert response.text == "mock response"
    rows = await _call_log_rows(tenant_a)
    assert [row.status for row in rows] == [LlmCallStatus.OK]


async def test_exhausted_timeouts_raise_and_are_logged(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE_SECONDS", "0.01")  # см. тест выше
    get_settings.cache_clear()
    provider = MockLlmProvider(timeouts_before_success=100)

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(SIMPLE_REQUEST, provider=provider)

    assert error.value.code == ERR_AI_PROVIDER_TIMEOUT
    assert error.value.status_code == 503
    assert len(provider.calls) == get_settings().llm_max_attempts

    rows = await _call_log_rows(tenant_a)
    assert [row.status for row in rows] == [LlmCallStatus.TIMEOUT]
    assert rows[0].cost_usd == Decimal(0)
    assert rows[0].prompt_hash == compute_prompt_hash(SIMPLE_REQUEST)


class _FailingProvider:
    """Провайдер, падающий не-таймаутной ошибкой: ретраев быть не должно."""

    name = "mock"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.call_count += 1
        raise LlmProviderError("invalid api key")


async def test_provider_error_is_not_retried_and_is_logged(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    provider = _FailingProvider()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await complete(SIMPLE_REQUEST, provider=provider)

    assert error.value.code == ERR_AI_PROVIDER_ERROR
    assert error.value.status_code == 502
    assert provider.call_count == 1  # не-таймаутная ошибка не ретраится

    rows = await _call_log_rows(tenant_a)
    assert [row.status for row in rows] == [LlmCallStatus.ERROR]


async def test_tenant_daily_budget_refuses_before_provider_call(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, tenant_b = two_tenants
    # Бюджет $1.5 при стоимости вызова $2: первый проходит (потрачено 0),
    # второй обязан быть отвергнут ДО обращения к провайдеру.
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "1.5")
    get_settings.cache_clear()
    provider = MockLlmProvider(input_tokens=200_000, output_tokens=40_000)

    with tenant_context(tenant_a):
        await complete(SIMPLE_REQUEST, provider=provider)
        with pytest.raises(AppError) as error:
            await complete(SIMPLE_REQUEST, provider=provider)

    assert error.value.code == ERR_AI_BUDGET_EXCEEDED
    assert error.value.status_code == 429
    assert error.value.headers is not None and "Retry-After" in error.value.headers
    assert len(provider.calls) == 1  # отказ не дошёл до провайдера

    # Затраты тенанта A не тратят бюджет тенанта B (RLS, P-4).
    with tenant_context(tenant_b):
        response = await complete(SIMPLE_REQUEST, provider=provider)
    assert response.cost_usd == Decimal("2")


async def test_budget_metrics_snapshot_covers_every_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #103: наружу отдаётся расход КАЖДОГО тенанта и его лимит.

    Отель, который сегодня не потратил ничего, обязан быть в снимке с нулём:
    иначе алерт о вчерашнем расходе некому погасить в новых сутках.
    """
    tenant_a, tenant_b = two_tenants
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "10.0")
    get_settings.cache_clear()
    provider = MockLlmProvider(input_tokens=200_000, output_tokens=40_000)  # $2 за вызов

    with tenant_context(tenant_a):
        await complete(SIMPLE_REQUEST, provider=provider)
    await refresh_budget_metrics()

    assert _gauge("llm_daily_spend_usd", tenant_a) == 2.0
    assert _gauge("llm_daily_spend_usd", tenant_b) == 0.0
    assert _gauge("llm_daily_budget_usd", tenant_a) == 10.0


async def test_budget_metrics_snapshot_is_empty_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступная БД не роняет /metrics и не выдаёт старый снимок за нынешний:
    метрики стираются, и для алертера это «не знаю» (issue #103)."""
    set_llm_daily_budget({str(uuid.uuid4()): (Decimal("9"), Decimal("10"))})

    def explode() -> None:
        raise RuntimeError("postgres is down")

    monkeypatch.setattr("hospitality.ai.gateway.service.platform_session_scope", explode)
    await refresh_budget_metrics()

    assert generate_latest().decode().count("llm_daily_spend_usd{") == 0
