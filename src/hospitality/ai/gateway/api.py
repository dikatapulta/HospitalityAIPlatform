"""Публичный API AI Gateway — единственная точка импорта извне (R-5, §7.2).

Канонический вызов LLM (P-12):

    from hospitality.ai.gateway import api as gateway

    with tenant_context(tenant_id):
        response = await gateway.complete(
            LlmRequest(messages=[LlmMessage(role="user", content="...")])
        )

Назначение `"mentor"` (ADR-024) — своя модель и свой бюджет; там же поток
(`stream`) и эмбеддинги (`embed`):

    async with aclosing(gateway.stream(LlmRequest(..., purpose="mentor"))) as events:
        async for event in events:
            ...

Остальные файлы пакета — приватные детали (анатомия §5.2, канон —
modules/requests). `MockLlmProvider` экспортируется как Fake-адаптер порта
(ADR-007) — им пользуются тесты зависимых слоёв (оркестратор, Task 0015).
"""

from __future__ import annotations

from hospitality.ai.gateway.embeddings import embed
from hospitality.ai.gateway.mock_provider import (
    MockEmbeddingProvider,
    MockLlmProvider,
    MockTurn,
    ScriptedLlmProvider,
    mock_embedding,
)
from hospitality.ai.gateway.outcomes import refresh_call_outcome_metrics
from hospitality.ai.gateway.price_list import EMBEDDING_DIMENSIONS
from hospitality.ai.gateway.provider import EmbeddingProvider, LlmProvider, LlmStreamingProvider
from hospitality.ai.gateway.providers import build_provider, validate_configured_model
from hospitality.ai.gateway.purposes import ERR_AI_PURPOSE_NOT_CONFIGURED, LlmPurpose
from hospitality.ai.gateway.schemas import (
    EmbeddingResponse,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmStreamCompleted,
    LlmStreamEvent,
    LlmTextDelta,
    ToolCall,
    ToolSpec,
)
from hospitality.ai.gateway.service import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PROVIDER_TIMEOUT,
    complete,
    compute_prompt_hash,
    refresh_budget_metrics,
)
from hospitality.ai.gateway.spend import spend_usd_between
from hospitality.ai.gateway.streaming import stream

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "ERR_AI_BUDGET_EXCEEDED",
    "ERR_AI_PROVIDER_ERROR",
    "ERR_AI_PROVIDER_TIMEOUT",
    "ERR_AI_PURPOSE_NOT_CONFIGURED",
    "EmbeddingProvider",
    "EmbeddingResponse",
    "LlmMessage",
    "LlmProvider",
    "LlmPurpose",
    "LlmRequest",
    "LlmResponse",
    "LlmStreamCompleted",
    "LlmStreamEvent",
    "LlmStreamingProvider",
    "LlmTextDelta",
    "MockEmbeddingProvider",
    "MockLlmProvider",
    "MockTurn",
    "ScriptedLlmProvider",
    "ToolCall",
    "ToolSpec",
    "build_provider",
    "complete",
    "compute_prompt_hash",
    "embed",
    "mock_embedding",
    "refresh_budget_metrics",
    "refresh_call_outcome_metrics",
    "spend_usd_between",
    "stream",
    "validate_configured_model",
]
