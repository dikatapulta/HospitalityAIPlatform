"""Адаптер OpenAI — единственное место импорта SDK `openai` (R-5, ADR-020).

Канон — `anthropic_provider.py`: одна модель, SDK-ретраи выключены
(`max_retries=0`), ретраи — один механизм в шлюзе. Вызов идёт через
Responses API, а не Chat Completions: у GPT-6 инструменты в Chat Completions
ограничены, в Responses работают на любой модели (ADR-020, «Какой API»).

Параметры запроса — решения ADR, а не умолчания SDK: без рассуждений,
без хранения ответа как состояния диалога (`store: false`; журналы abuse
monitoring OpenAI держит до 30 дней и так — ADR-020 §3), без кэша промпта (его
записи у GPT-5.6+ платные, а порт не несёт токенов кэша — стоимость по
прайс-листу перестала бы сходиться со счётом провайдера) и с тарифом
`service_tier: "default"` (ADR-024 §6): иначе тариф берётся из настроек проекта
OpenAI — Fast стоит 2×, Flex — 0,5×, и стоимость молча разошлась бы со счётом.

Один класс реализует три порта: `LlmProvider` (`complete`), `LlmStreamingProvider`
(`stream`) и `EmbeddingProvider` (`embed`) — на одном клиенте (ADR-024 §6).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterator
from typing import Any, Final

import openai
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseInputParam,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
)

from hospitality.ai.gateway.provider import (
    LlmProviderEmbeddings,
    LlmProviderError,
    LlmProviderResult,
    LlmProviderStreamEnd,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmRequest, ToolCall

# Пачка эмбеддингов: у OpenAI до 2048 текстов и до 300K токенов на запрос. Символов
# — с запасом под 2 символа на токен (оценка ADR-017): весь кодекс пилота (~75K
# токенов, spec 0048 §8) уходит одной пачкой.
_EMBED_BATCH_TEXTS: Final = 2048
_EMBED_BATCH_CHARS: Final = 400_000


class OpenAIProvider:
    """Боевой адаптер портов чата, потока и эмбеддингов поверх API OpenAI."""

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
                input=_input(request),
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
                service_tier="default",
            )
        # Порядок важен: APITimeoutError — подкласс APIConnectionError/APIError.
        except openai.APITimeoutError as error:
            raise LlmProviderTimeoutError(str(error)) from error
        except openai.APIError as error:
            raise LlmProviderError(str(error)) from error
        return self._to_result(response)

    async def stream(self, request: LlmRequest) -> AsyncGenerator[str | LlmProviderStreamEnd, None]:
        """Поток Responses API: куски `output_text` (и `refusal`), затем итог с `usage`.

        Инструментов в потоке нет — их запрещает шлюз (ADR-024 §5). Ошибки SDK
        посреди потока приходят теми же `APITimeoutError` / `APIError`.
        Закрытие генератора закрывает и HTTP-ответ (`async with`).
        """
        try:
            events = await self._client.responses.create(
                stream=True,
                model=self._model,
                instructions=request.system if request.system is not None else openai.omit,
                input=_input(request),
                max_output_tokens=request.max_tokens,
                reasoning={"effort": "none"},
                store=False,
                prompt_cache_options={"mode": "explicit"},
                service_tier="default",
            )
            async with events:
                async for event in events:
                    if isinstance(event, ResponseTextDeltaEvent | ResponseRefusalDeltaEvent):
                        yield event.delta
                    elif isinstance(event, ResponseCompletedEvent | ResponseIncompleteEvent):
                        yield self._stream_end(event.response)
                        return
                    elif isinstance(event, ResponseFailedEvent):
                        error = event.response.error
                        raise LlmProviderError(
                            f"{error.code}: {error.message}" if error else "response.failed"
                        )
                    elif isinstance(event, ResponseErrorEvent):
                        raise LlmProviderError(f"{event.code}: {event.message}")
        except openai.APITimeoutError as error:
            raise LlmProviderTimeoutError(str(error)) from error
        except openai.APIError as error:
            raise LlmProviderError(str(error)) from error
        # Поток кончился без итогового события: расход неизвестен — шлюз запишет
        # стоимость резерва, а не ноль (ADR-024 §5).
        yield LlmProviderStreamEnd(model=self._model)

    async def embed(self, texts: list[str]) -> LlmProviderEmbeddings:
        vectors: list[list[float]] = []
        input_tokens = 0
        for batch in _embedding_batches(texts):
            try:
                response = await self._client.embeddings.create(
                    model=self._model, input=batch, encoding_format="float"
                )
            except openai.APITimeoutError as error:
                raise LlmProviderTimeoutError(str(error)) from error
            except openai.APIError as error:
                raise LlmProviderError(str(error)) from error
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in ordered)
            input_tokens += response.usage.prompt_tokens
        return LlmProviderEmbeddings(vectors=vectors, model=self._model, input_tokens=input_tokens)

    def _stream_end(self, response: Response) -> LlmProviderStreamEnd:
        usage = response.usage
        details = response.incomplete_details
        return LlmProviderStreamEnd(
            model=self._model,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            stop_reason=details.reason if details is not None else response.status,
        )

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


def _input(request: LlmRequest) -> ResponseInputParam:
    return [{"role": message.role, "content": message.content} for message in request.messages]


def _embedding_batches(texts: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and (len(batch) == _EMBED_BATCH_TEXTS or size + len(text) > _EMBED_BATCH_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


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
