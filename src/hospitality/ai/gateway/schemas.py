"""Pydantic-схемы границ AI Gateway (Task 0014/0015, FOUNDATION §7.2, R-6).

`LlmRequest` — то, что вызывающая сторона (оркестратор, Task 0015) хочет
сказать модели; `LlmResponse` — ответ плюс учётные данные вызова (токены,
стоимость, latency, хэш промпта), уже записанные в `LlmCallLog`.

Инструменты (Task 0015, §7.3): `ToolSpec` — объявление инструмента для модели
(имя, описание, JSON Schema входа); `ToolCall` — запрос модели вызвать
инструмент. Класс подтверждения (P-9) здесь НЕ живёт: это свойство инструмента
в `ai/tools`, оркестратор читает его отдельно. Gateway несёт только то, что
нужно провайдеру.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class LlmMessage(BaseModel):
    """Одна реплика диалога. Роли — как в Messages API Anthropic."""

    role: Literal["user", "assistant"]
    content: str


class ToolSpec(BaseModel):
    """Объявление инструмента для модели (§7.3, провайдер-facing).

    `input_schema` — JSON Schema аргументов; строится вызывающей стороной на
    запросе (например, `enum` доступных категорий тенанта), поэтому не хранится
    статически.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    """Запрос модели вызвать инструмент (блок `tool_use` ответа Anthropic)."""

    id: str
    name: str
    arguments: dict[str, Any]


class LlmRequest(BaseModel):
    """Запрос к LLM. Системная инструкция отделена от реплик (§7.6:
    системные инструкции и пользовательский контент строго разделены)."""

    messages: list[LlmMessage] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=1024, gt=0)
    # Набор инструментов — часть «версии промпта» (§7.2): входит в prompt_hash.
    tools: list[ToolSpec] = Field(default_factory=list)


class LlmResponse(BaseModel):
    """Ответ модели + учёт вызова (строка `LlmCallLog` с id `call_id`)."""

    call_id: uuid.UUID
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    prompt_hash: str
    # Инструменты, которые модель просит вызвать (пусто — обычный текстовый ответ).
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str | None = None
