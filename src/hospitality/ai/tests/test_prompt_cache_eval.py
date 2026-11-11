"""Обвязка платного замера кэша `evals/prompt_cache` — бесплатно, на Fake (issue #138)."""

from __future__ import annotations

import pytest

from hospitality.ai.evals import prompt_cache
from hospitality.ai.evals.bakeoff import _ensure_eval_tenant
from hospitality.ai.gateway.api import MockLlmProvider


@pytest.mark.usefixtures("canonical_database", "service_requests_enabled")
async def test_two_turns_carry_cache_point_and_report_cache_tokens() -> None:
    await _ensure_eval_tenant()
    provider = MockLlmProvider(text="ответ", input_tokens=5000, cache_read_input_tokens=4000)

    first, second = await prompt_cache.measure(provider)

    assert len(first) == 1
    assert [result.cache_read_input_tokens for result in second] == [4000]
    # Оба хода — с одной и той же стабильной частью и точкой кэша после неё.
    prefixes = {request.system_cache_prefix for request in provider.calls}
    assert len(prefixes) == 1 and None not in prefixes
    assert "# Hotel facts" in (provider.calls[0].system_cache_prefix or "")
