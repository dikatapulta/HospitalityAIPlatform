"""Task 0018: LLM-метрики §10.7 пишутся в единой точке `_log_call`.

Счётчики глобальны на процесс — тесты сравнивают приращения.
"""

from __future__ import annotations

import uuid

import pytest
from prometheus_client import REGISTRY

from hospitality.ai.gateway.api import LlmMessage, LlmRequest, MockLlmProvider, complete
from hospitality.ai.gateway.mock_provider import DEFAULT_MOCK_MODEL
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")

SIMPLE_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])


def _sample(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return value if value is not None else 0.0


async def test_successful_call_records_calls_tokens_and_cost(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    calls_labels = {
        "tenant_id": str(tenant_a),
        "model": DEFAULT_MOCK_MODEL,
        "status": "ok",
    }
    cost_labels = {"tenant_id": str(tenant_a), "model": DEFAULT_MOCK_MODEL}
    tokens_labels = dict(cost_labels, direction="input")
    calls_before = _sample("llm_calls_total", calls_labels)
    cost_before = _sample("llm_cost_usd_total", cost_labels)
    tokens_before = _sample("llm_tokens_total", tokens_labels)

    provider = MockLlmProvider(input_tokens=200_000, output_tokens=40_000)
    with tenant_context(tenant_a):
        response = await complete(SIMPLE_REQUEST, provider=provider)

    assert _sample("llm_calls_total", calls_labels) == calls_before + 1
    assert _sample("llm_cost_usd_total", cost_labels) == pytest.approx(
        cost_before + float(response.cost_usd)
    )
    assert _sample("llm_tokens_total", tokens_labels) == tokens_before + 200_000


async def test_timeout_is_recorded_with_status_label(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    labels = {
        "tenant_id": str(tenant_a),
        # На путях timeout/error модель провайдера неизвестна — журнал и метрики
        # используют настройку LLM_MODEL (как `_log_call`).
        "model": get_settings().llm_model,
        "status": "timeout",
    }
    before = _sample("llm_calls_total", labels)

    provider = MockLlmProvider(timeouts_before_success=100)
    with tenant_context(tenant_a), pytest.raises(AppError):
        await complete(SIMPLE_REQUEST, provider=provider)

    assert _sample("llm_calls_total", labels) == before + 1
