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
from hospitality.ai.gateway.provider import (
    LlmProviderError,
    LlmProviderTimeoutError,
    LlmProviderTransientError,
)
from hospitality.ai.gateway.schemas import LlmMessage, LlmRequest, ToolSpec

_HTTPX_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

SIMPLE_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])


class _StubAsyncAnthropic:
    """Заглушка anthropic.AsyncAnthropic: фиксирует kwargs и отдаёт сценарий."""

    last_instance: _StubAsyncAnthropic | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.client_kwargs = kwargs
        self.create_kwargs: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.include_tool_use = False
        _StubAsyncAnthropic.last_instance = self
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        if self.error is not None:
            raise self.error
        content = [
            SimpleNamespace(type="thinking", thinking="…"),
            SimpleNamespace(type="text", text="Здравствуйте! "),
            SimpleNamespace(type="text", text="Чем помочь?"),
        ]
        if self.include_tool_use:
            content.append(
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_1",
                    name="create_service_request",
                    input={"category_key": "housekeeping", "summary": "убрать номер"},
                )
            )
        return SimpleNamespace(
            model="server-reported-model-string",
            content=content,
            usage=SimpleNamespace(input_tokens=42, output_tokens=7),
            stop_reason="tool_use" if self.include_tool_use else "end_turn",
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


async def test_passes_tools_and_parses_tool_use(stub_sdk: type[_StubAsyncAnthropic]) -> None:
    provider = _provider()
    stub = stub_sdk.last_instance
    assert stub is not None
    stub.include_tool_use = True
    request = LlmRequest(
        messages=[LlmMessage(role="user", content="уберите номер 305")],
        tools=[
            ToolSpec(
                name="create_service_request",
                description="Оформить заявку службе отеля.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
    )

    result = await provider.complete(request)

    assert stub.create_kwargs is not None
    assert stub.create_kwargs["tools"] == [
        {
            "name": "create_service_request",
            "description": "Оформить заявку службе отеля.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    # Текст и вызов инструмента возвращаются вместе; stop_reason проброшен.
    assert result.text == "Здравствуйте! Чем помочь?"
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.name == "create_service_request"
    assert call.arguments == {"category_key": "housekeeping", "summary": "убрать номер"}


async def test_forced_tool_translates_to_tool_choice(
    stub_sdk: type[_StubAsyncAnthropic],
) -> None:
    # Именно эта строка делает «свободный текст невозможен» реальностью на
    # боевом API (гейт P-9, Task 0017.1): без tool_choice модель может ответить
    # текстом, и классификация уйдёт в fallback `other`.
    request = LlmRequest(
        messages=[LlmMessage(role="user", content="да")],
        tools=[
            ToolSpec(
                name="resolve_confirmation",
                description="Классифицировать ответ гостя на подтверждение.",
                input_schema={"type": "object", "properties": {}},
            )
        ],
        forced_tool="resolve_confirmation",
    )

    await _provider().complete(request)

    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    assert stub.create_kwargs["tool_choice"] == {"type": "tool", "name": "resolve_confirmation"}


async def test_omits_tool_choice_when_no_forced_tool(
    stub_sdk: type[_StubAsyncAnthropic],
) -> None:
    await _provider().complete(SIMPLE_REQUEST)
    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    assert stub.create_kwargs["tool_choice"] is anthropic.omit


async def test_omits_tools_when_none_given(stub_sdk: type[_StubAsyncAnthropic]) -> None:
    await _provider().complete(LlmRequest(messages=[LlmMessage(role="user", content="Hi")]))
    stub = stub_sdk.last_instance
    assert stub is not None and stub.create_kwargs is not None
    assert stub.create_kwargs["tools"] is anthropic.omit


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


def _status(
    error_class: type[anthropic.APIStatusError], status_code: int, headers: dict[str, str]
) -> anthropic.APIStatusError:
    response = httpx.Response(status_code, headers=headers, request=_HTTPX_REQUEST)
    return error_class(f"Error code: {status_code}", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "transient", "kind", "retry_after"),
    [
        (anthropic.APIConnectionError(request=_HTTPX_REQUEST), True, "connection", None),
        (_status(anthropic.RateLimitError, 429, {"retry-after": "2"}), True, "rate_limit", 2.0),
        # Без `retry-after` — месячный потолок тарифа (`enforced_spend_limit_reached`).
        (_status(anthropic.RateLimitError, 429, {}), False, "rate_limit", None),
        # 529 у SDK — не подкласс InternalServerError: адаптер смотрит на код ответа.
        (_status(anthropic.OverloadedError, 529, {}), True, "overloaded", None),
        (_status(anthropic.InternalServerError, 500, {}), True, "server", None),
        (_status(anthropic.AuthenticationError, 401, {}), False, "auth", None),
        (_status(anthropic.BadRequestError, 400, {}), False, "request", None),
    ],
    ids=["connection", "429-retry-after", "429-cap", "529", "500", "401", "400"],
)
async def test_api_error_maps_to_transient_or_permanent_port_error(
    stub_sdk: type[_StubAsyncAnthropic],
    error: anthropic.APIError,
    transient: bool,
    kind: str,
    retry_after: float | None,
) -> None:
    """Какой отказ временный — решает адаптер; повторяет его шлюз (#409)."""
    provider = _provider()
    assert stub_sdk.last_instance is not None
    stub_sdk.last_instance.error = error

    with pytest.raises(LlmProviderError) as raised:
        await provider.complete(SIMPLE_REQUEST)

    assert isinstance(raised.value, LlmProviderTransientError) is transient
    assert raised.value.kind == kind
    if isinstance(raised.value, LlmProviderTransientError):
        assert raised.value.retry_after_seconds == retry_after


def test_empty_api_key_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key="", model="claude-opus-4-8", timeout_seconds=7.0)
