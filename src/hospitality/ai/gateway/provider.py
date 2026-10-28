"""Порт LLM-провайдера (Task 0014, FOUNDATION §7.2, P-3).

Адаптер провайдера отвечает только за перевод `LlmRequest` в вызов SDK
и ответа SDK — в `LlmProviderResult`. Ретраи, бюджет, стоимость и журнал —
забота `service.py`: адаптер о них не знает, поэтому у каждого провайдера
они работают одинаково.

Ошибки порта — не `AppError`: это внутренний контракт gateway, в коды
каталога их переводит `service.py` (ERR-AI-001/003).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

from pydantic import BaseModel, Field

from hospitality.ai.gateway.schemas import LlmRequest, ToolCall


class LlmProviderResult(BaseModel):
    """Сырой результат провайдера: текст, инструменты и фактический расход токенов.

    `model` — модель, с которой сконфигурирован адаптер (а не строка из
    ответа API): по ней `service.py` детерминированно считает стоимость.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str | None = None


class LlmProviderTimeoutError(Exception):
    """Провайдер не ответил за таймаут — единственная ретраебельная ошибка."""


class LlmProviderError(Exception):
    """Любая другая ошибка провайдера (HTTP-ошибка API, сеть, невалидный ключ)."""


class LlmProvider(Protocol):
    """Контракт адаптера LLM-провайдера (порт, P-3).

    Реализации: боевые `AnthropicProvider` и `OpenAIProvider` (ADR-020; какая
    из них — решает модель, `providers.build_provider`) и `MockLlmProvider`
    (Fake-адаптер для dev/CI, ADR-007).
    """

    @property
    def name(self) -> str:
        """Короткое имя провайдера для журнала и логов ("anthropic", "openai", "mock")."""
        ...

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        """Один вызов модели. Таймаут — `LlmProviderTimeoutError`,
        прочие сбои — `LlmProviderError`."""
        ...


class LlmProviderStreamEnd(BaseModel):
    """Последний элемент потока провайдера: модель и расход, если провайдер их прислал.

    `None` в токенах — поток закончился без `usage`. Шлюз пишет тогда стоимость
    резерва, а не ноль: вызов оплачен, а сколько — неизвестно (ADR-024 §5).
    """

    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None


class LlmStreamingProvider(Protocol):
    """Поток ответа модели (ADR-024 §5) — отдельный протокол, а не метод `LlmProvider`.

    Иначе адаптер Anthropic обязан был бы реализовать то, чего никто не зовёт
    (NG-8). Реализации: `OpenAIProvider` и `MockLlmProvider`.
    """

    @property
    def name(self) -> str: ...

    def stream(self, request: LlmRequest) -> AsyncGenerator[str | LlmProviderStreamEnd, None]:
        """Куски текста по мере написания, последним — `LlmProviderStreamEnd`.

        Ошибки — те же, что у `complete()`, в любой момент потока. Закрытие
        итератора потребителем обязано закрыть соединение с провайдером.
        """
        ...


class LlmProviderEmbeddings(BaseModel):
    """Векторы в порядке входа и оплаченные токены входа."""

    vectors: list[list[float]]
    model: str
    input_tokens: int


class EmbeddingProvider(Protocol):
    """Эмбеддинги текстов (ADR-024 §4). У Anthropic такого API нет, поэтому —
    отдельный протокол. Реализации: `OpenAIProvider` и `MockEmbeddingProvider`.

    Деление на пачки по лимитам провайдера — внутри адаптера: один вызов шлюза —
    одна строка журнала.
    """

    @property
    def name(self) -> str: ...

    async def embed(self, texts: list[str]) -> LlmProviderEmbeddings: ...
