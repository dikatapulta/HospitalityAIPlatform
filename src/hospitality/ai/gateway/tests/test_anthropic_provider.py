"""Контрактный тест анфропик-адаптера порта LlmProvider (Task 0014, R-7).

SDK-клиент подменяется заглушкой (сети в тестах нет): проверяется перевод
`LlmRequest` → вызов Messages API и ответа/ошибок SDK → типы порта.
БД не нужна — адаптер о журнале и бюджете не знает (это забота service.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from hospitality.ai.gateway.anthropic_provider import AnthropicProvider
from hospitality.ai.gateway.provider import LlmProviderError, LlmProviderTimeoutError
from hospitality.ai.gateway.schemas import LlmMessage, LlmRequest

_HTTPX_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

SIMPLE_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])


class _StubAsyncAnthropic:
    """Заглушка anthropic.AsyncAnthropic: фиксирует kwargs и отдаёт сценарий."""

    last_instance: _StubAsyncAnthropic | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.client_kwargs = kwargs
        self.create_kwargs: dict[str, Any] | None = None
        self.error: Exception | None = None
        _StubAsyncAnthropic.last_instance = self
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            model="server-reported-model-string",
            content=[
                SimpleNamespace(type="thinking", thinking="…"),
                SimpleNamespace(type="text", text="Здравствуйте! "),
                SimpleNamespace(type="text", text="Чем помочь?"),
            ],
            usage=SimpleNamespace(input_tokens=42, output_tokens=7),
        )


@pytest.fixture
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> type[_StubAsyncAnthropic]:
    monkeypatch.setattr(
        "hospitality.ai.gateway.anthropic_provider.anthropic.AsyncAnthropic",
        _StubAsyncAnthropic,
    )
    _StubAsyncAnthropic.last_instance = None
    return _StubAsyncAnthropic


def _provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key", model="claude-opus-4-8", timeout_seconds=7.0)


async def test_translates_request_and_response(stub_sdk: type[_StubAsyncAnthropic]) -> None:
    provider = _provider()
    request = LlmRequest(
        messages=[LlmMessage(role="user", content="Привет!")],
        system="Ты — консьерж отеля.",
        max_tokens=256,
    )

    result = await provider.complete(request)

    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    # SDK-ретраи выключены: единственный механизм ретраев — gateway (service.py).
    assert stub.client_kwargs["max_retries"] == 0
    assert stub.client_kwargs["timeout"] == 7.0
    assert stub.create_kwargs["model"] == "claude-opus-4-8"
    assert stub.create_kwargs["max_tokens"] == 256
    assert stub.create_kwargs["system"] == "Ты — консьерж отеля."
    assert stub.create_kwargs["messages"] == [{"role": "user", "content": "Привет!"}]

    # Текст — конкатенация text-блоков; модель — сконфигурированная, не строка
    # из ответа API (по ней service.py детерминированно считает стоимость).
    assert result.text == "Здравствуйте! Чем помочь?"
    assert result.model == "claude-opus-4-8"
    assert result.input_tokens == 42
    assert result.output_tokens == 7


async def test_omits_system_when_not_given(stub_sdk: type[_StubAsyncAnthropic]) -> None:
    await _provider().complete(LlmRequest(messages=[LlmMessage(role="user", content="Hi")]))
    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    assert stub.create_kwargs["system"] is anthropic.omit


async def test_timeout_maps_to_port_timeout_error(stub_sdk: type[_StubAsyncAnthropic]) -> None:
    provider = _provider()
    assert stub_sdk.last_instance is not None
    stub_sdk.last_instance.error = anthropic.APITimeoutError(_HTTPX_REQUEST)

    with pytest.raises(LlmProviderTimeoutError):
        await provider.complete(SIMPLE_REQUEST)


async def test_api_error_maps_to_port_provider_error(stub_sdk: type[_StubAsyncAnthropic]) -> None:
    provider = _provider()
    assert stub_sdk.last_instance is not None
    stub_sdk.last_instance.error = anthropic.APIConnectionError(request=_HTTPX_REQUEST)

    with pytest.raises(LlmProviderError):
        await provider.complete(SIMPLE_REQUEST)


def test_empty_api_key_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key="", model="claude-opus-4-8", timeout_seconds=7.0)
