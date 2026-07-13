"""Fake-адаптер порта LLM-провайдера (ADR-007): рождается в одном PR с портом.

Полноценная локальная реализация для dev/CI/демо: детерминированный ответ,
настраиваемые токены и число «таймаутов» перед успехом; копит полученные
запросы — тесты проверяют и ретраи, и то, что бюджетный отказ не доходит
до провайдера.
"""

from __future__ import annotations

from hospitality.ai.gateway.provider import LlmProviderResult, LlmProviderTimeoutError
from hospitality.ai.gateway.schemas import LlmRequest

# Модель по умолчанию совпадает с боевой (Settings.llm_model): у Fake-адаптера
# та же строка прайс-листа, стоимость в журнале ненулевая — как в проде.
DEFAULT_MOCK_MODEL = "claude-opus-4-8"


class MockLlmProvider:
    """Fake-реализация порта `LlmProvider` (ADR-007)."""

    name = "mock"

    def __init__(
        self,
        *,
        text: str = "mock response",
        model: str = DEFAULT_MOCK_MODEL,
        input_tokens: int = 200,
        output_tokens: int = 100,
        timeouts_before_success: int = 0,
    ) -> None:
        self._text = text
        self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._remaining_timeouts = timeouts_before_success
        self.calls: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.calls.append(request)
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise LlmProviderTimeoutError("mock provider timeout")
        return LlmProviderResult(
            text=self._text,
            model=self._model,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
