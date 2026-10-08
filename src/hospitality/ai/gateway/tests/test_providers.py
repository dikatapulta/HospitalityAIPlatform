"""Провайдер выводится из модели по прайс-листу (ADR-020, R-7).

Сети нет: адаптеры создаются, а вызов идёт в заглушку SDK. БД не нужна.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hospitality.ai.gateway.api import build_provider
from hospitality.ai.gateway.price_list import MODEL_PRICING_USD_PER_MTOK, MODEL_PROVIDER
from hospitality.ai.gateway.tests.test_anthropic_provider import _StubAsyncAnthropic
from hospitality.ai.gateway.tests.test_openai_provider import SIMPLE_REQUEST, _StubAsyncOpenAI
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


@pytest.mark.parametrize(
    ("model", "sdk_path", "stub_sdk"),
    [
        (
            "claude-haiku-4-5",
            "hospitality.ai.gateway.anthropic_provider.anthropic.AsyncAnthropic",
            _StubAsyncAnthropic,
        ),
        (
            "gpt-5.6-luna",
            "hospitality.ai.gateway.openai_provider.openai.AsyncOpenAI",
            _StubAsyncOpenAI,
        ),
    ],
    ids=["anthropic", "openai"],
)
async def test_built_provider_calls_the_named_model_not_llm_model(
    provider_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    sdk_path: str,
    stub_sdk: type[_StubAsyncAnthropic] | type[_StubAsyncOpenAI],
) -> None:
    """Bake-off строит провайдера под каждого кандидата через ту же дверь: адаптер
    под чужой `LLM_MODEL` молча гонял бы модель гостя под именем кандидата и
    считал бы цену по ней (ревью #424, Н-7, Н-12). По модели на каждую ветку
    `build_provider`; `LLM_MODEL` закреплена, чтобы не зависеть от `.env`."""
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5")
    get_settings.cache_clear()
    monkeypatch.setattr(sdk_path, stub_sdk)
    stub_sdk.last_instance = None

    await build_provider(model).complete(SIMPLE_REQUEST)

    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    assert stub.create_kwargs["model"] == model


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
