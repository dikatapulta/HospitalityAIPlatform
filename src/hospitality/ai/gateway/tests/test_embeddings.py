"""Эмбеддинги шлюза (ADR-024 §4; spec 0048 §11, PR 2) на Fake-провайдере."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    EMBEDDING_DIMENSIONS,
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PURPOSE_NOT_CONFIGURED,
    MockEmbeddingProvider,
    embed,
    mock_embedding,
)
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import LlmProviderEmbeddings
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")


@pytest.fixture
def mentor_enabled() -> Iterator[None]:
    """Назначение `mentor` включено. Свой `MonkeyPatch.context()`, а не общий
    `monkeypatch`: иначе откат окружения зависел бы от порядка фикстур, и временная
    БД `canonical_database` удалялась бы раньше отката `POSTGRES_DB` (tests/conftest.py)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_MENTOR", "gpt-6-luna")
        patch.setenv("LLM_EMBEDDING_MODEL", "text-embedding-3-small")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


async def _rows(tenant_id: uuid.UUID) -> list[LlmCallLog]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(LlmCallLog)))


@pytest.mark.usefixtures("mentor_enabled")
async def test_embed_returns_vectors_in_order_and_logs_cost(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    texts = ["Статья 30. Встреча гостя", "Статья 31. Жалоба"]

    with tenant_context(tenant_a):
        response = await embed(texts, purpose="mentor", provider=MockEmbeddingProvider())

    assert response.vectors == [mock_embedding(text) for text in texts]
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in response.vectors)
    # Mock: 41 символ / 2 → 21 токен; $0,02 за 1M.
    assert response.input_tokens == 21
    assert response.cost_usd == Decimal(21) * Decimal("0.02") / Decimal(1_000_000)

    [row] = await _rows(tenant_a)
    assert row.id == response.call_id
    assert (row.status, row.purpose, row.output_tokens) == (LlmCallStatus.OK, "mentor", 0)
    assert row.cost_usd == response.cost_usd.quantize(Decimal("0.000001"))
    with tenant_context(tenant_a):
        async with session_scope() as session:
            assert list(await session.scalars(select(LlmBudgetReservation))) == []


@pytest.mark.usefixtures("mentor_enabled")
async def test_malformed_vectors_are_refused_but_paid_call_is_logged(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Вектор не той длины — ERR-AI-003, а оплаченный ответ виден бюджету (#426 §1)."""
    tenant_a, _ = two_tenants

    class _ShortVectors:
        name = "mock"

        async def embed(self, texts: list[str]) -> LlmProviderEmbeddings:
            return LlmProviderEmbeddings(
                vectors=[[0.0] * 3 for _ in texts],
                model="text-embedding-3-small",
                input_tokens=1_000_000,
            )

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await embed(["Статья 30"], purpose="mentor", provider=_ShortVectors())

    assert error.value.code == ERR_AI_PROVIDER_ERROR
    [row] = await _rows(tenant_a)
    assert (row.status, row.cost_usd) == (LlmCallStatus.ERROR, Decimal("0.02"))


async def test_embed_for_disabled_purpose_is_refused(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("LLM_MODEL_MENTOR", "")
    get_settings.cache_clear()
    provider = MockEmbeddingProvider()

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await embed(["Статья 30"], purpose="mentor", provider=provider)

    assert error.value.code == ERR_AI_PURPOSE_NOT_CONFIGURED
    assert provider.calls == []
    assert await _rows(tenant_a) == []


@pytest.mark.parametrize("texts", [[], ["Статья 30", ""]])
async def test_empty_input_is_a_caller_error(texts: list[str]) -> None:
    with pytest.raises(ValueError, match="непустой"):
        await embed(texts, purpose="guest", provider=MockEmbeddingProvider())


def test_mock_embedding_is_deterministic_and_unit_length() -> None:
    vector = mock_embedding("Статья 30")
    assert vector == mock_embedding("Статья 30")
    assert vector != mock_embedding("Статья 31")
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9
