"""Контрактный тест адаптера OpenAI порта LlmProvider (ADR-020, R-7).

Канон — test_anthropic_provider.py: SDK-клиент подменяется заглушкой (сети в
тестах нет), проверяется перевод `LlmRequest` → вызов Responses API и
ответа/ошибок SDK → типы порта. БД не нужна — адаптер о журнале и бюджете не знает.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.provider import LlmProviderError, LlmProviderTimeoutError
from hospitality.ai.gateway.schemas import LlmMessage, LlmRequest, ToolSpec

_HTTPX_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")

SIMPLE_REQUEST = LlmRequest(messages=[LlmMessage(role="user", content="Привет!")])

_TOOL = ToolSpec(
    name="create_service_request",
    description="Оформить заявку службе отеля.",
    input_schema={"type": "object", "properties": {}},
)


def _message(*parts: Any) -> SimpleNamespace:
    return SimpleNamespace(type="message", content=list(parts))


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="output_text", text=text)


def _function_call(arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id="call_1",
        name="create_service_request",
        arguments=arguments,
    )


class _StubAsyncOpenAI:
    """Заглушка openai.AsyncOpenAI: фиксирует kwargs и отдаёт сценарий."""

    last_instance: _StubAsyncOpenAI | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.client_kwargs = kwargs
        self.create_kwargs: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.output: list[Any] = [
            SimpleNamespace(type="reasoning", summary=[]),
            _message(_text("Здравствуйте! "), _text("Чем помочь?")),
        ]
        self.usage: Any = SimpleNamespace(input_tokens=42, output_tokens=7)
        self.response_error: Any = None
        self.incomplete_details: Any = None
        _StubAsyncOpenAI.last_instance = self
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            model="server-reported-model-string",
            output=self.output,
            usage=self.usage,
            error=self.response_error,
            incomplete_details=self.incomplete_details,
            status="incomplete" if self.incomplete_details is not None else "completed",
        )


@pytest.fixture
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> type[_StubAsyncOpenAI]:
    monkeypatch.setattr(
        "hospitality.ai.gateway.openai_provider.openai.AsyncOpenAI", _StubAsyncOpenAI
    )
    _StubAsyncOpenAI.last_instance = None
    return _StubAsyncOpenAI


def _provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key", model="gpt-6-luna", timeout_seconds=7.0)


def _stub(stub_sdk: type[_StubAsyncOpenAI]) -> _StubAsyncOpenAI:
    stub = stub_sdk.last_instance
    assert stub is not None
    return stub


async def test_translates_request_and_response(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    provider = _provider()
    request = LlmRequest(
        messages=[
            LlmMessage(role="user", content="Привет!"),
            LlmMessage(role="assistant", content="Здравствуйте!"),
            LlmMessage(role="user", content="Когда завтрак?"),
        ],
        system="Ты — консьерж отеля.",
        max_tokens=256,
    )

    result = await provider.complete(request)

    stub = _stub(stub_sdk)
    assert stub.create_kwargs is not None
    # SDK-ретраи выключены: единственный механизм ретраев — gateway (service.py).
    assert stub.client_kwargs["max_retries"] == 0
    assert stub.client_kwargs["timeout"] == 7.0
    assert stub.create_kwargs["model"] == "gpt-6-luna"
    assert stub.create_kwargs["max_output_tokens"] == 256
    assert stub.create_kwargs["instructions"] == "Ты — консьерж отеля."
    assert stub.create_kwargs["input"] == [
        {"role": "user", "content": "Привет!"},
        {"role": "assistant", "content": "Здравствуйте!"},
        {"role": "user", "content": "Когда завтрак?"},
    ]
    # Решения ADR-020: без рассуждений, без хранения ответа как состояния диалога
    # (журналы abuse monitoring у OpenAI остаются — §3), без кэша.
    assert stub.create_kwargs["reasoning"] == {"effort": "none"}
    assert stub.create_kwargs["store"] is False
    assert stub.create_kwargs["prompt_cache_options"] == {"mode": "explicit"}

    # Текст — конкатенация output_text всех сообщений; reasoning-элементы
    # пропускаются; модель — сконфигурированная, не строка из ответа API.
    assert result.text == "Здравствуйте! Чем помочь?"
    assert result.model == "gpt-6-luna"
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.tool_calls == []
    assert result.stop_reason == "completed"


async def test_passes_tools_and_parses_function_call(
    stub_sdk: type[_StubAsyncOpenAI],
) -> None:
    provider = _provider()
    stub = _stub(stub_sdk)
    stub.output = [
        _message(_text("Оформляю.")),
        _function_call('{"category_key": "housekeeping", "summary": "убрать номер"}'),
    ]
    request = LlmRequest(
        messages=[LlmMessage(role="user", content="уберите номер 305")], tools=[_TOOL]
    )

    result = await provider.complete(request)

    assert stub.create_kwargs is not None
    # strict=False: схема уходит как есть, без переписывания в strict-режим.
    assert stub.create_kwargs["tools"] == [
        {
            "type": "function",
            "name": "create_service_request",
            "description": "Оформить заявку службе отеля.",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    ]
    assert result.text == "Оформляю."
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "create_service_request"
    assert call.arguments == {"category_key": "housekeeping", "summary": "убрать номер"}


async def test_forced_tool_translates_to_tool_choice(
    stub_sdk: type[_StubAsyncOpenAI],
) -> None:
    # Гейт P-9 (Task 0017.1) держится на том, что свободный текст невозможен.
    request = LlmRequest(
        messages=[LlmMessage(role="user", content="да")],
        tools=[_TOOL],
        forced_tool="create_service_request",
    )

    await _provider().complete(request)

    stub = _stub(stub_sdk)
    assert stub.create_kwargs is not None
    assert stub.create_kwargs["tool_choice"] == {
        "type": "function",
        "name": "create_service_request",
    }


async def test_omits_optional_fields_when_not_given(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    await _provider().complete(SIMPLE_REQUEST)

    stub = _stub(stub_sdk)
    assert stub.create_kwargs is not None
    assert stub.create_kwargs["instructions"] is openai.omit
    assert stub.create_kwargs["tools"] is openai.omit
    assert stub.create_kwargs["tool_choice"] is openai.omit


async def test_refusal_is_returned_as_text(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    """Отказ модели у OpenAI — отдельная часть сообщения; у порта это просто текст."""
    provider = _provider()
    _stub(stub_sdk).output = [_message(SimpleNamespace(type="refusal", refusal="Не могу."))]

    result = await provider.complete(SIMPLE_REQUEST)

    assert result.text == "Не могу."


async def test_unknown_message_part_type_is_skipped(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    """Новый тип части у API — не AttributeError мимо порта: SDK не валидирует
    ответ, и незнакомая часть приходит объектом без `text`/`refusal` (ревью #424, Н-1)."""
    provider = _provider()
    _stub(stub_sdk).output = [
        _message(_text("Завтрак "), SimpleNamespace(type="future_part"), _text("с 7:00."))
    ]

    result = await provider.complete(SIMPLE_REQUEST)

    assert result.text == "Завтрак с 7:00."


async def test_incomplete_response_reports_reason(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    provider = _provider()
    _stub(stub_sdk).incomplete_details = SimpleNamespace(reason="max_output_tokens")

    result = await provider.complete(SIMPLE_REQUEST)

    assert result.stop_reason == "max_output_tokens"


@pytest.mark.parametrize("arguments", ["{не json", '["housekeeping"]'])
async def test_unreadable_tool_arguments_map_to_provider_error(
    stub_sdk: type[_StubAsyncOpenAI], arguments: str
) -> None:
    provider = _provider()
    _stub(stub_sdk).output = [_function_call(arguments)]

    with pytest.raises(LlmProviderError, match="create_service_request"):
        await provider.complete(SIMPLE_REQUEST)


async def test_response_error_maps_to_provider_error(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    provider = _provider()
    _stub(stub_sdk).response_error = SimpleNamespace(code="server_error", message="boom")

    with pytest.raises(LlmProviderError, match="server_error"):
        await provider.complete(SIMPLE_REQUEST)


async def test_missing_usage_maps_to_provider_error(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    """Без usage стоимость не посчитать — молча записанный ноль ослепил бы бюджет."""
    provider = _provider()
    _stub(stub_sdk).usage = None

    with pytest.raises(LlmProviderError, match="usage"):
        await provider.complete(SIMPLE_REQUEST)


async def test_timeout_maps_to_port_timeout_error(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    provider = _provider()
    _stub(stub_sdk).error = openai.APITimeoutError(_HTTPX_REQUEST)

    with pytest.raises(LlmProviderTimeoutError):
        await provider.complete(SIMPLE_REQUEST)


async def test_api_error_maps_to_port_provider_error(stub_sdk: type[_StubAsyncOpenAI]) -> None:
    provider = _provider()
    _stub(stub_sdk).error = openai.APIConnectionError(request=_HTTPX_REQUEST)

    with pytest.raises(LlmProviderError):
        await provider.complete(SIMPLE_REQUEST)


def test_empty_api_key_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIProvider(api_key="", model="gpt-6-luna", timeout_seconds=7.0)
