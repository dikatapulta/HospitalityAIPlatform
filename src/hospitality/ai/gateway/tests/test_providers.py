"""Провайдер выводится из модели по прайс-листу (ADR-020, R-7).

Сети нет: адаптеры только создаются, вызовов модели нет. БД не нужна.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hospitality.ai.gateway.api import build_provider
from hospitality.ai.gateway.price_list import MODEL_PRICING_USD_PER_MTOK, MODEL_PROVIDER
from hospitality.shared.config import get_settings


@pytest.fixture
def provider_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("model", sorted(MODEL_PRICING_USD_PER_MTOK))
def test_every_priced_model_is_served_by_its_provider(provider_keys: None, model: str) -> None:
    """Группа прайс-листа без адаптера — модель, с которой процесс стартует, но не
    может ответить гостю. Тест ловит это на каждой строке таблицы."""
    provider = build_provider(model)

    assert provider.name == MODEL_PROVIDER[model]


def test_model_outside_price_list_is_not_built(provider_keys: None) -> None:
    with pytest.raises(ValueError, match="MODEL_PRICING_USD_PER_MTOK"):
        build_provider("claude-sonnet-5-20250929")


def test_missing_key_of_the_models_provider_is_configuration_error(
    provider_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_provider("gpt-6-luna")
