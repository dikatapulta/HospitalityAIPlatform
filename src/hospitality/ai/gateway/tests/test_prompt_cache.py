"""Кэш промпта в шлюзе (issue #138, R-7): контракт запроса, цена и журнал.

Точку кэша в запросе к Anthropic проверяет контрактный тест адаптера
(`test_anthropic_provider.py`), границу в гостевом ходе — тест оркестратора
(`ai/tests/test_hotel_facts_block.py`). Здесь — то, что между ними: поле запроса,
доли входа по цене кэша, строка журнала.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hospitality.ai.gateway.api import (
    LlmMessage,
    LlmRequest,
    MockLlmProvider,
    complete,
    compute_prompt_hash,
)
from hospitality.ai.gateway.models import LlmCallLog
from hospitality.ai.gateway.price_list import (
    MODEL_CACHE_PRICING_USD_PER_MTOK,
    MODEL_PRICING_USD_PER_MTOK,
    MODEL_PROVIDER,
)
from hospitality.ai.gateway.provider import LlmProviderResult
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context

_STABLE = "Ты — консьерж отеля."
_TURN = "\n\nКомната 305."
_MESSAGES = [LlmMessage(role="user", content="Привет!")]


def test_cache_prefix_must_start_system() -> None:
    """Префикс, которого нет в начале `system`, — ошибка вызывающего, а не тихий промах."""
    with pytest.raises(ValidationError):
        LlmRequest(messages=_MESSAGES, system=_STABLE + _TURN, system_cache_prefix=_TURN)
    with pytest.raises(ValidationError):
        LlmRequest(messages=_MESSAGES, system=None, system_cache_prefix=_STABLE)
    with pytest.raises(ValidationError):
        LlmRequest(messages=_MESSAGES, system=_STABLE, system_cache_prefix="")


def test_cache_prefix_does_not_change_prompt_hash() -> None:
    """Модель видит тот же текст — версия промпта прежняя (§7.2)."""
    plain = LlmRequest(messages=_MESSAGES, system=_STABLE + _TURN)
    cached = LlmRequest(messages=_MESSAGES, system=_STABLE + _TURN, system_cache_prefix=_STABLE)

    assert compute_prompt_hash(cached) == compute_prompt_hash(plain)


def test_cache_tokens_are_a_share_of_input() -> None:
    with pytest.raises(ValidationError):
        LlmProviderResult(
            text="",
            model="claude-sonnet-5",
            input_tokens=100,
            output_tokens=1,
            cache_creation_input_tokens=60,
            cache_read_input_tokens=60,
        )


@pytest.mark.parametrize(
    "model", sorted(m for m, provider in MODEL_PROVIDER.items() if provider == "anthropic")
)
def test_every_anthropic_model_has_cache_prices(model: str) -> None:
    """Адаптер Anthropic отдаёт токены кэша — без строки цены шлюз уронил бы оплаченный ход."""
    write_price, read_price = MODEL_CACHE_PRICING_USD_PER_MTOK[model]
    input_price, _ = MODEL_PRICING_USD_PER_MTOK[model]

    assert read_price < input_price < write_price


@pytest.mark.usefixtures("canonical_database")
async def test_cache_shares_are_priced_and_logged(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Sonnet 5: вход $2, запись в кэш $2,50, чтение $0,20, выход $10 за Mtok.

    1M входа = 200K по обычной цене + 200K записи + 600K чтения:
    $0,40 + $0,50 + $0,12, и 100K выхода — $1. Без кэша тот же вызов стоил бы $3.
    """
    tenant_a, _ = two_tenants
    provider = MockLlmProvider(
        model="claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=200_000,
        cache_read_input_tokens=600_000,
    )
    request = LlmRequest(messages=_MESSAGES, system=_STABLE + _TURN, system_cache_prefix=_STABLE)

    with tenant_context(tenant_a):
        response = await complete(request, provider=provider)
        async with session_scope() as session:
            row = await session.scalar(select(LlmCallLog).where(LlmCallLog.id == response.call_id))

    assert provider.calls == [request]
    assert response.cost_usd == Decimal("2.02")
    assert response.input_tokens == 1_000_000
    assert response.cache_creation_input_tokens == 200_000
    assert response.cache_read_input_tokens == 600_000
    assert row is not None
    assert row.input_tokens == 1_000_000
    assert row.cache_creation_input_tokens == 200_000
    assert row.cache_read_input_tokens == 600_000
    assert row.cost_usd == Decimal("2.02")
