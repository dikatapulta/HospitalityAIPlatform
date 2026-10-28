"""Контракт адаптера OpenAI для потока и эмбеддингов (ADR-024 §4–6, R-7).

Канон — test_openai_provider.py: клиент SDK подменяется заглушкой, сети нет.
События потока — настоящие классы SDK (`model_construct`), потому что адаптер
разбирает их по `isinstance`: заглушка другой формы прошла бы мимо всех веток.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseTextDeltaEvent,
)

from hospitality.ai.gateway import openai_provider
from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.provider import (
    LlmProviderError,
    LlmProviderStreamEnd,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmMessage, LlmRequest

REQUEST = LlmRequest(
    messages=[LlmMessage(role="user", content="Как встречать гостя?")],
    system="Ты — наставник.",
    max_tokens=400,
    purpose="mentor",
)
_HTTPX_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def _delta(text: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent.model_construct(type="response.output_text.delta", delta=text)


def _response(usage: Any, *, status: str = "completed", reason: str | None = None) -> Any:
    details = SimpleNamespace(reason=reason) if reason is not None else None
    return SimpleNamespace(usage=usage, incomplete_details=details, status=status, error=None)


class _Events:
    """Ответ `responses.create(stream=True)`: `async with` + `async for`."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.closed = False

    async def __aenter__(self) -> _Events:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def __aiter__(self) -> AsyncIterator[Any]:
        for item in self._items:
            if isinstance(item, Exception):
                raise item
            yield item


class _StubAsyncOpenAI:
    last_instance: _StubAsyncOpenAI | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.create_kwargs: dict[str, Any] | None = None
        self.events = _Events([])
        self.embedding_calls: list[list[str]] = []
        _StubAsyncOpenAI.last_instance = self
        self.responses = SimpleNamespace(create=self._create)
        self.embeddings = SimpleNamespace(create=self._embed)

    async def _create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        return self.events

    async def _embed(self, *, model: str, input: list[str], encoding_format: str) -> Any:
        self.embedding_calls.append(list(input))
        # Порядок data намеренно обратный: адаптер обязан сортировать по index.
        data = [
            SimpleNamespace(index=index, embedding=[float(len(text))])
            for index, text in reversed(list(enumerate(input)))
        ]
        return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=len(input) * 10))


@pytest.fixture
def stub() -> Any:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("hospitality.ai.gateway.openai_provider.openai.AsyncOpenAI", _StubAsyncOpenAI)
        provider = OpenAIProvider(api_key="test-key", model="gpt-6-luna", timeout_seconds=7.0)
        client = _StubAsyncOpenAI.last_instance
        assert client is not None
        yield provider, client


async def _collect(provider: OpenAIProvider) -> list[str | LlmProviderStreamEnd]:
    return [item async for item in provider.stream(REQUEST)]


async def test_stream_translates_request_and_yields_chunks_then_usage(stub: Any) -> None:
    provider, client = stub
    usage = SimpleNamespace(input_tokens=10_000, output_tokens=400)
    client.events = _Events(
        [
            _delta("Улыбнитесь "),
            _delta("и поздоровайтесь."),
            ResponseCompletedEvent.model_construct(
                type="response.completed", response=_response(usage)
            ),
        ]
    )

    items = await _collect(provider)

    assert items[:2] == ["Улыбнитесь ", "и поздоровайтесь."]
    assert items[2] == LlmProviderStreamEnd(
        model="gpt-6-luna", input_tokens=10_000, output_tokens=400, stop_reason="completed"
    )
    assert client.events.closed
    kwargs = client.create_kwargs
    assert kwargs is not None
    assert kwargs["stream"] is True
    assert kwargs["instructions"] == "Ты — наставник."
    assert kwargs["max_output_tokens"] == 400
    # Решения ADR-020 и ADR-024 §6: без рассуждений, хранения и кэша, тариф default.
    assert kwargs["reasoning"] == {"effort": "none"}
    assert kwargs["store"] is False
    assert kwargs["prompt_cache_options"] == {"mode": "explicit"}
    assert kwargs["service_tier"] == "default"
    assert "tools" not in kwargs


async def test_incomplete_stream_ends_with_reason(stub: Any) -> None:
    provider, client = stub
    usage = SimpleNamespace(input_tokens=5, output_tokens=400)
    client.events = _Events(
        [
            _delta("Длинный"),
            ResponseIncompleteEvent.model_construct(
                type="response.incomplete",
                response=_response(usage, status="incomplete", reason="max_output_tokens"),
            ),
        ]
    )

    items = await _collect(provider)

    end = items[-1]
    assert isinstance(end, LlmProviderStreamEnd)
    assert (end.output_tokens, end.stop_reason) == (400, "max_output_tokens")


async def test_stream_without_final_event_ends_without_usage(stub: Any) -> None:
    """Обрыв со стороны провайдера без итога — шлюз запишет стоимость резерва."""
    provider, client = stub
    client.events = _Events([_delta("Да")])

    items = await _collect(provider)

    assert items == ["Да", LlmProviderStreamEnd(model="gpt-6-luna")]


@pytest.mark.parametrize(
    "event",
    [
        ResponseFailedEvent.model_construct(
            type="response.failed",
            response=SimpleNamespace(error=SimpleNamespace(code="server_error", message="boom")),
        ),
        ResponseErrorEvent.model_construct(type="error", code="rate_limit", message="slow down"),
    ],
)
async def test_failure_events_become_port_errors(stub: Any, event: Any) -> None:
    provider, client = stub
    client.events = _Events([_delta("Начал"), event])

    with pytest.raises(LlmProviderError):
        await _collect(provider)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request=_HTTPX_REQUEST), LlmProviderTimeoutError),
        (openai.APIConnectionError(request=_HTTPX_REQUEST), LlmProviderError),
    ],
)
async def test_sdk_errors_mid_stream_map_to_port_errors(
    stub: Any, error: Exception, expected: type[Exception]
) -> None:
    provider, client = stub
    client.events = _Events([_delta("Начал"), error])

    with pytest.raises(expected):
        await _collect(provider)


async def test_embed_keeps_input_order_and_sums_usage_over_batches(
    stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, client = stub
    monkeypatch.setattr(openai_provider, "_EMBED_BATCH_TEXTS", 2)
    texts = ["а", "бб", "ввв"]

    result = await provider.embed(texts)

    assert client.embedding_calls == [["а", "бб"], ["ввв"]]  # пачки по лимиту адаптера
    assert result.vectors == [[1.0], [2.0], [3.0]]  # порядок входа, не порядок data
    assert result.input_tokens == 30
    assert result.model == "gpt-6-luna"


def test_embedding_batches_respect_the_char_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_provider, "_EMBED_BATCH_CHARS", 5)
    batches = list(openai_provider._embedding_batches(["abc", "de", "f", "123456"]))
    assert batches == [["abc", "de"], ["f"], ["123456"]]
