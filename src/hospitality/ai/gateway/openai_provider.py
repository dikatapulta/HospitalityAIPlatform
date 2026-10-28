"""Адаптер OpenAI — единственное место импорта SDK `openai` (R-5, ADR-020).

Канон — `anthropic_provider.py`: одна модель, SDK-ретраи выключены
(`max_retries=0`), ретраи — один механизм в `service.py`. Вызов идёт через
Responses API, а не Chat Completions: у GPT-6 инструменты в Chat Completions
ограничены, в Responses работают на любой модели (ADR-020, «Какой API»).

Три параметра запроса — решения ADR-020, а не умолчания SDK: без рассуждений,
без хранения ответа как состояния диалога (`store: false`; журналы abuse
monitoring OpenAI держит до 30 дней и так — ADR-020 §3) и без кэша промпта (его
записи у GPT-5.6+ платные, а порт не несёт токенов кэша — стоимость по
прайс-листу перестала бы сходиться со счётом провайдера).
"""

from __future__ import annotations

import json
from typing import Any

import openai
from openai.types.responses import Response, ResponseFunctionToolCall

from hospitality.ai.gateway.provider import (
    LlmProviderError,
    LlmProviderResult,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmRequest, ToolCall


class OpenAIProvider:
    """Боевой адаптер порта `LlmProvider` поверх Responses API OpenAI."""

    name = "openai"

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None:
        # Пустой ключ — ошибка конфигурации: падаем при создании адаптера, а не
        # 401 на первом вызове гостя (до него не доходит — fail-fast старта, #372).
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set: провайдер OpenAI требует ключ "
                "(docs/runbooks/secrets.md); для тестов используйте MockLlmProvider"
            )
        self._model = model
        self._client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        try:
            response = await self._client.responses.create(
                model=self._model,
                instructions=request.system if request.system is not None else openai.omit,
                input=[
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                max_output_tokens=request.max_tokens,
                tools=(
                    [
                        {
                            "type": "function",
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                            # Схема как есть, без переписывания в strict-режим —
                            # та же семантика, что у Anthropic (ADR-020 §2).
                            "strict": False,
                        }
                        for tool in request.tools
                    ]
                    if request.tools
                    else openai.omit
                ),
                tool_choice=(
                    {"type": "function", "name": request.forced_tool}
                    if request.forced_tool is not None
                    else openai.omit
                ),
                reasoning={"effort": "none"},
                store=False,
                # explicit без точек кэша — запрос идёт без кэша (ADR-020 §5).
                prompt_cache_options={"mode": "explicit"},
            )
        # Порядок важен: APITimeoutError — подкласс APIConnectionError/APIError.
        except openai.APITimeoutError as error:
            raise LlmProviderTimeoutError(str(error)) from error
        except openai.APIError as error:
            raise LlmProviderError(str(error)) from error
        return self._to_result(response)

    def _to_result(self, response: Response) -> LlmProviderResult:
        if response.error is not None:
            raise LlmProviderError(f"{response.error.code}: {response.error.message}")
        if response.usage is None:
            # Без usage стоимость вызова не посчитать, а молча записать ноль —
            # ослепить дневной бюджет (§7.2).
            raise LlmProviderError("OpenAI response has no usage: стоимость вызова неизвестна")
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in response.output:
            if item.type == "message":
                # Части перечисляются по известным типам, как в каноне: SDK разбирает
                # ответ без валидации, и незнакомый тип пришёл бы объектом без
                # `refusal` — AttributeError мимо порта, гость без ответа.
                for part in item.content:
                    if part.type == "output_text":
                        text_parts.append(part.text)
                    elif part.type == "refusal":
                        text_parts.append(part.refusal)
            elif item.type == "function_call":
                tool_calls.append(
                    ToolCall(id=item.call_id, name=item.name, arguments=_arguments(item))
                )
        details = response.incomplete_details
        return LlmProviderResult(
            text="".join(text_parts),
            # Сконфигурированная модель, а не response.model: стоимость в
            # service.py считается детерминированно по прайс-листу.
            model=self._model,
            input_tokens=response.usage.input_tokens,
            # Токены рассуждений входят в output_tokens и оплачиваются как выход.
            output_tokens=response.usage.output_tokens,
            tool_calls=tool_calls,
            stop_reason=details.reason if details is not None else response.status,
        )


def _arguments(call: ResponseFunctionToolCall) -> dict[str, Any]:
    """Аргументы вызова: у OpenAI — JSON-строка, у порта — объект.

    Нечитаемая строка — сбой провайдера, а не исключение посреди хода гостя:
    шлюз переводит его в ERR-AI-003 и пишет исход в журнал.
    """
    try:
        arguments = json.loads(call.arguments)
    except json.JSONDecodeError as error:
        raise LlmProviderError(f"tool call {call.name!r}: arguments are not JSON") from error
    if not isinstance(arguments, dict):
        raise LlmProviderError(f"tool call {call.name!r}: arguments are not a JSON object")
    return arguments
