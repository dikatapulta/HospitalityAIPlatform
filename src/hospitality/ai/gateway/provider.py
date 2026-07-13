"""Порт LLM-провайдера (Task 0014, FOUNDATION §7.2, P-3).

Адаптер провайдера отвечает только за перевод `LlmRequest` в вызов SDK
и ответа SDK — в `LlmProviderResult`. Ретраи, бюджет, стоимость и журнал —
забота `service.py`: адаптер о них не знает, поэтому у каждого провайдера
они работают одинаково.

Ошибки порта — не `AppError`: это внутренний контракт gateway, в коды
каталога их переводит `service.py` (ERR-AI-001/003).
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from hospitality.ai.gateway.schemas import LlmRequest


class LlmProviderResult(BaseModel):
    """Сырой результат провайдера: текст и фактический расход токенов.

    `model` — модель, с которой сконфигурирован адаптер (а не строка из
    ответа API): по ней `service.py` детерминированно считает стоимость.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int


class LlmProviderTimeoutError(Exception):
    """Провайдер не ответил за таймаут — единственная ретраебельная ошибка."""


class LlmProviderError(Exception):
    """Любая другая ошибка провайдера (HTTP-ошибка API, сеть, невалидный ключ)."""


class LlmProvider(Protocol):
    """Контракт адаптера LLM-провайдера (порт, P-3).

    Реализации: `AnthropicProvider` (боевая) и `MockLlmProvider`
    (Fake-адаптер для dev/CI, ADR-007).
    """

    @property
    def name(self) -> str:
        """Короткое имя провайдера для журнала и логов ("anthropic", "mock")."""
        ...

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        """Один вызов модели. Таймаут — `LlmProviderTimeoutError`,
        прочие сбои — `LlmProviderError`."""
        ...
