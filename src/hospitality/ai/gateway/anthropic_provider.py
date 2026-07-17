"""Адаптер Anthropic — единственное место импорта SDK `anthropic` (R-5).

Импорт-линтер (контракт 4 pyproject.toml) отлавливает `import anthropic`
где угодно ещё. Одна модель из настроек, без маршрутизации (Non-Goal
Task 0014). SDK-ретраи выключены (`max_retries=0`): ретраи — один
канонический механизм в `service.py`, а не два конкурирующих.
"""

from __future__ import annotations

from typing import Any, cast

import anthropic

from hospitality.ai.gateway.provider import (
    LlmProviderError,
    LlmProviderResult,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmRequest, ToolCall


class AnthropicProvider:
    """Боевой адаптер порта `LlmProvider` поверх Messages API Anthropic."""

    name = "anthropic"

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None:
        # Пустой ключ — ошибка конфигурации окружения: падаем при создании
        # адаптера, а не таймаутом/401 на первом вызове гостя.
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set: провайдер Anthropic требует ключ "
                "(docs/runbooks/secrets.md); для тестов используйте MockLlmProvider"
            )
        self._model = model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=request.max_tokens,
                system=request.system if request.system is not None else anthropic.omit,
                messages=[
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                tools=(
                    [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": tool.input_schema,
                        }
                        for tool in request.tools
                    ]
                    if request.tools
                    else anthropic.omit
                ),
                tool_choice=(
                    {"type": "tool", "name": request.forced_tool}
                    if request.forced_tool is not None
                    else anthropic.omit
                ),
            )
        # Порядок важен: APITimeoutError — подкласс APIConnectionError/APIError.
        except anthropic.APITimeoutError as error:
            raise LlmProviderTimeoutError(str(error)) from error
        except anthropic.APIError as error:
            raise LlmProviderError(str(error)) from error
        text = "".join(block.text for block in response.content if block.type == "text")
        tool_calls = [
            # block.input типизирован SDK как object, но это всегда JSON-объект.
            ToolCall(id=block.id, name=block.name, arguments=cast("dict[str, Any]", block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        return LlmProviderResult(
            text=text,
            # Сконфигурированная модель, а не response.model: стоимость в
            # service.py считается детерминированно по прайс-листу.
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
        )
