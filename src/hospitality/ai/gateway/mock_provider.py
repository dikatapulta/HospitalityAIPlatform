"""Fake-адаптеры порта LLM-провайдера (ADR-007): рождаются в одном PR с портом.

Полноценные локальные реализации для dev/CI/демо: поведение модели не
симулируется (исключение §8, правило 7), это сценарные стабы для тестов и evals.

- `MockLlmProvider` — один настраиваемый ответ (текст и/или tool_calls),
  число «таймаутов» перед успехом; копит запросы. Канон Task 0014.
- `ScriptedLlmProvider` — последовательность ответов по ходам диалога:
  оркестратор (Task 0015) двухходовой (предложение → подтверждение), поэтому
  тесту нужен разный ответ на первом и втором вызове.
- `MockLlmProvider.stream` — тот же ответ потоком по словам (ADR-024 §5).
- `MockEmbeddingProvider` — детерминированные векторы: одинаковый текст — один
  и тот же вектор, разные тексты — разные (ADR-024 §4).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from hospitality.ai.gateway.price_list import EMBEDDING_DIMENSIONS
from hospitality.ai.gateway.provider import (
    LlmProviderEmbeddings,
    LlmProviderResult,
    LlmProviderStreamEnd,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmRequest, ToolCall

# Модель по умолчанию — из боевого прайс-листа (price_list.py): у Fake
# та же строка стоимости, ненулевая стоимость в журнале — как в проде. Это
# стабильная константа тестов, не привязана к текущему Settings.llm_model.
DEFAULT_MOCK_MODEL = "claude-opus-4-8"


class MockLlmProvider:
    """Fake-реализация порта `LlmProvider` (ADR-007) — один сценарный ответ."""

    name = "mock"

    def __init__(
        self,
        *,
        text: str = "mock response",
        tool_calls: list[ToolCall] | None = None,
        model: str = DEFAULT_MOCK_MODEL,
        input_tokens: int = 200,
        output_tokens: int = 100,
        timeouts_before_success: int = 0,
    ) -> None:
        self._text = text
        self._tool_calls = tool_calls or []
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
            tool_calls=self._tool_calls,
            stop_reason="tool_use" if self._tool_calls else "end_turn",
        )

    async def stream(self, request: LlmRequest) -> AsyncGenerator[str | LlmProviderStreamEnd, None]:
        """Порт `LlmStreamingProvider`: текст ответа по словам, затем итог с расходом."""
        self.calls.append(request)
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise LlmProviderTimeoutError("mock provider timeout")
        for chunk in re.findall(r"\S+\s*", self._text):
            yield chunk
        yield LlmProviderStreamEnd(
            model=self._model,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            stop_reason="end_turn",
        )


# Модель эмбеддингов по умолчанию — из боевой группы прайс-листа, как у чата.
DEFAULT_MOCK_EMBEDDING_MODEL = "text-embedding-3-small"


class MockEmbeddingProvider:
    """Fake-реализация порта `EmbeddingProvider` (ADR-007).

    Вектор — из sha256 текста, единичной длины: поиск по косинусу находит
    ровно тот же текст, остальные для него «далёкие». Токенов входа — символов / 2,
    как в оценке резерва (ADR-017), чтобы стоимость в журнале была ненулевой.
    """

    name = "mock"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MOCK_EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
        timeouts_before_success: int = 0,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._remaining_timeouts = timeouts_before_success
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> LlmProviderEmbeddings:
        self.calls.append(list(texts))
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise LlmProviderTimeoutError("mock provider timeout")
        return LlmProviderEmbeddings(
            vectors=[mock_embedding(text, self._dimensions) for text in texts],
            model=self._model,
            input_tokens=math.ceil(sum(len(text) for text in texts) / 2),
        )


def mock_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Детерминированный единичный вектор текста — тот же, что отдаёт `MockEmbeddingProvider`."""
    raw: list[float] = []
    block = 0
    while len(raw) < dimensions:
        digest = hashlib.sha256(f"{block}:{text}".encode()).digest()
        raw.extend(byte / 127.5 - 1.0 for byte in digest)
        block += 1
    vector = raw[:dimensions]
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


@dataclass
class MockTurn:
    """Один сценарный ход: текст и/или запрошенные инструменты."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class ScriptedLlmProvider:
    """Fake-провайдер, отдающий заранее заданные ходы по порядку (ADR-007).

    Для многоходовых сценариев оркестратора (предложение → подтверждение):
    каждый вызов `complete()` берёт следующий `MockTurn`. Исчерпание сценария —
    ошибка теста (не молчаливый повтор).
    """

    name = "mock"

    def __init__(
        self,
        turns: list[MockTurn],
        *,
        model: str = DEFAULT_MOCK_MODEL,
        input_tokens: int = 200,
        output_tokens: int = 100,
    ) -> None:
        self._turns = list(turns)
        self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self.calls.append(request)
        if not self._turns:
            raise AssertionError("ScriptedLlmProvider: сценарий исчерпан, а вызван ещё раз")
        turn = self._turns.pop(0)
        return LlmProviderResult(
            text=turn.text,
            model=self._model,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            tool_calls=turn.tool_calls,
            stop_reason="tool_use" if turn.tool_calls else "end_turn",
        )
