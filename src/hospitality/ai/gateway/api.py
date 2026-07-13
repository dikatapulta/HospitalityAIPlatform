"""Публичный API AI Gateway — единственная точка импорта извне (R-5, §7.2).

Канонический вызов LLM (P-12):

    from hospitality.ai.gateway import api as gateway

    with tenant_context(tenant_id):
        response = await gateway.complete(
            LlmRequest(messages=[LlmMessage(role="user", content="...")])
        )

Остальные файлы пакета — приватные детали (анатомия §5.2, канон —
modules/requests). `MockLlmProvider` экспортируется как Fake-адаптер порта
(ADR-007) — им пользуются тесты зависимых слоёв (оркестратор, Task 0015).
"""

from __future__ import annotations

from hospitality.ai.gateway.mock_provider import MockLlmProvider
from hospitality.ai.gateway.provider import LlmProvider
from hospitality.ai.gateway.schemas import LlmMessage, LlmRequest, LlmResponse
from hospitality.ai.gateway.service import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PROVIDER_TIMEOUT,
    complete,
    compute_prompt_hash,
)

__all__ = [
    "ERR_AI_BUDGET_EXCEEDED",
    "ERR_AI_PROVIDER_ERROR",
    "ERR_AI_PROVIDER_TIMEOUT",
    "LlmMessage",
    "LlmProvider",
    "LlmRequest",
    "LlmResponse",
    "MockLlmProvider",
    "complete",
    "compute_prompt_hash",
]
