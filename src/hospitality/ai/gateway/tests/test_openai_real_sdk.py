"""Адаптер OpenAI на настоящем SDK: сеть подменяет `httpx2.MockTransport` (#426 §3, R-7).

Заглушка клиента (`test_openai_provider.py`) проверяет перевод типов, но форму SDK
подделывает сама — изменение SDK в пределах `<4` она пропустит. Здесь SDK настоящий:
он сериализует запрос и разбирает JSON ответа сам, а тест видит тело запроса
и итог на порту.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest

from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.provider import LlmProviderError
from hospitality.ai.gateway.schemas import LlmMessage, LlmRequest, ToolSpec

REQUEST = LlmRequest(
    messages=[LlmMessage(role="user", content="Уберите номер, пожалуйста")],
    system="Ты — консьерж отеля.",
    max_tokens=300,
    tools=[
        ToolSpec(
            name="create_service_request",
            description="Оформить заявку службе отеля.",
            input_schema={"type": "object", "properties": {"category_key": {"type": "string"}}},
        )
    ],
)

_USAGE = {
    "input_tokens": 3800,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens": 180,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 3980,
}


def _body(output: list[dict[str, Any]], *, status: str = "completed") -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": "gpt-6-luna-2026-09-01",
        "output": output,
        "usage": _USAGE,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def _function_call(arguments: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "create_service_request",
        "arguments": arguments,
        "status": "completed",
    }


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """Подключает ответ к настоящему клиенту SDK; возвращает тела отправленных запросов."""

    def install(body: dict[str, Any]) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=body)

        real_client = openai.AsyncOpenAI

        def client(**kwargs: Any) -> openai.AsyncOpenAI:
            transport = httpx2.MockTransport(handler)
            return real_client(**kwargs, http_client=httpx2.AsyncClient(transport=transport))

        monkeypatch.setattr("hospitality.ai.gateway.openai_provider.openai.AsyncOpenAI", client)
        return requests

    return install


def _provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key", model="gpt-6-luna", timeout_seconds=7.0)


async def test_real_sdk_request_and_response_translate(
    sent: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    requests = sent(
        _body(
            [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Оформляю.", "annotations": []}],
                },
                _function_call('{"category_key": "housekeeping"}'),
            ]
        )
    )

    result = await _provider().complete(REQUEST)

    assert result.text == "Оформляю."
    assert [(call.name, call.arguments) for call in result.tool_calls] == [
        ("create_service_request", {"category_key": "housekeeping"})
    ]
    assert (result.model, result.input_tokens, result.output_tokens) == ("gpt-6-luna", 3800, 180)
    (body,) = requests
    # Решения ADR-020 §2–5 и ADR-024 §6 доходят до провайдера в теле запроса.
    assert body["store"] is False
    assert body["service_tier"] == "default"
    assert body["reasoning"] == {"effort": "none"}
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert body["max_output_tokens"] == 300
    assert body["tools"][0]["strict"] is False


async def test_real_sdk_truncated_arguments_are_a_billed_error(
    sent: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    """Сценарий ревью PR #424: ответ обрезан `max_output_tokens` посреди аргументов.

    Вызов оплачен — ошибка порта несёт `usage` ответа, и шлюз запишет его стоимость,
    а не ноль (#426 §1).
    """
    sent(_body([_function_call('{"category_key": "housekee')], status="incomplete"))

    with pytest.raises(LlmProviderError, match="not JSON") as raised:
        await _provider().complete(REQUEST)

    assert raised.value.billed
    assert (raised.value.input_tokens, raised.value.output_tokens) == (3800, 180)
