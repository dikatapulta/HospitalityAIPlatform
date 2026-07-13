"""Pydantic-схемы границ AI Gateway (Task 0014, FOUNDATION §7.2, R-6).

`LlmRequest` — то, что вызывающая сторона (оркестратор, Task 0015) хочет
сказать модели; `LlmResponse` — ответ плюс учётные данные вызова (токены,
стоимость, latency, хэш промпта), уже записанные в `LlmCallLog`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class LlmMessage(BaseModel):
    """Одна реплика диалога. Роли — как в Messages API Anthropic."""

    role: Literal["user", "assistant"]
    content: str


class LlmRequest(BaseModel):
    """Запрос к LLM. Системная инструкция отделена от реплик (§7.6:
    системные инструкции и пользовательский контент строго разделены)."""

    messages: list[LlmMessage] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=1024, gt=0)


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
