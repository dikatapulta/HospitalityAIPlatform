"""Порт LLM-провайдера (Task 0014, FOUNDATION §7.2, P-3).

Адаптер провайдера отвечает только за перевод `LlmRequest` в вызов SDK
и ответа SDK — в `LlmProviderResult`. Ретраи, бюджет, стоимость и журнал —
забота `service.py`: адаптер о них не знает, поэтому у каждого провайдера
они работают одинаково.

Ошибки порта — не `AppError`: это внутренний контракт gateway, в коды
каталога их переводит `service.py` (ERR-AI-001/003). Какой HTTP-отказ
временный, решает одна таблица на оба адаптера — `status_error` (#409).
"""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator, Mapping
from typing import Literal, Protocol

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
    """Провайдер не ответил за таймаут — шлюз повторяет (ERR-AI-001 после последней попытки)."""


# Класс отказа — для лога `llm_call_failed` и рунбука ERR-AI-003: по нему видно,
# повтор не помог (`rate_limit`, `overloaded`, `server`, `connection`) или не мог
# помочь (`auth`, `billing`, `request`). `other` — ответ, который адаптер не смог
# прочитать. Пустой баланс Anthropic — 400, то есть `request`: различает его текст.
LlmProviderErrorKind = Literal[
    "connection", "rate_limit", "overloaded", "server", "auth", "billing", "request", "other"
]


class LlmProviderError(Exception):
    """Ошибка провайдера, которую повтор не исправит: ключ, баланс, сам запрос."""

    def __init__(self, message: str, *, kind: LlmProviderErrorKind = "other") -> None:
        super().__init__(message)
        self.kind: LlmProviderErrorKind = kind


class LlmProviderTransientError(LlmProviderError):
    """Временный отказ: обрыв соединения, 429 с `retry-after`, 5xx и 529 (#409).

    Шлюз повторяет его той же лестницей попыток, что и таймаут; `retry_after_seconds`
    — сколько просил подождать провайдер. Подкласс `LlmProviderError`: кто повторять
    не вправе (поток после первого куска), ловит его как обычную ошибку.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: LlmProviderErrorKind,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, kind=kind)
        self.retry_after_seconds = retry_after_seconds


def status_error(message: str, status_code: int, headers: Mapping[str, str]) -> LlmProviderError:
    """HTTP-отказ провайдера → ошибка порта: общая таблица обоих адаптеров (#409).

    429 временный, только если провайдер сказал, когда повторить: без `retry-after`
    приходит месячный потолок тарифа Anthropic (`enforced_spend_limit_reached`),
    который до 1-го числа не пройдёт (docs/runbooks/alerts.md, ERR-OPS-009).
    """
    retry_after = retry_after_seconds(headers)
    if status_code == 429:
        if retry_after is None:
            return LlmProviderError(message, kind="rate_limit")
        return LlmProviderTransientError(
            message, kind="rate_limit", retry_after_seconds=retry_after
        )
    if status_code >= 500:
        kind: LlmProviderErrorKind = "overloaded" if status_code == 529 else "server"
        return LlmProviderTransientError(message, kind=kind, retry_after_seconds=retry_after)
    if status_code in (401, 403):
        return LlmProviderError(message, kind="auth")
    if status_code == 402:
        return LlmProviderError(message, kind="billing")
    return LlmProviderError(message, kind="request")


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Пауза, которую просит провайдер: `retry-after-ms`, иначе `retry-after` в секундах.

    Тот же порядок, что у SDK обоих провайдеров. Дата HTTP вместо числа и негодное
    значение — `None`: провайдеры шлют секунды, а 429 без паузы шлюз не повторяет.
    """
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(raw) / scale
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            return value
    return None


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
        """Один вызов модели. Таймаут — `LlmProviderTimeoutError`, временный
        отказ — `LlmProviderTransientError`, прочие сбои — `LlmProviderError`."""
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
