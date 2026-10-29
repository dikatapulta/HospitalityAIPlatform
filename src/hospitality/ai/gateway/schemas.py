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

import hashlib
import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hospitality.ai.gateway.purposes import LlmPurpose


class LlmImage(BaseModel):
    """Картинка во входе модели (spec 0049 Р-13): тип и байты.

    Байты не сериализуются (`exclude`): в хэш промпта и в оценку резерва идёт
    только `sha256`, а в журнал и логи — ничего. Картинки умеет только адаптер
    из `IMAGE_PROVIDERS` прайс-листа.
    """

    model_config = ConfigDict(frozen=True)

    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    data: bytes = Field(min_length=1, exclude=True, repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class LlmMessage(BaseModel):
    """Одна реплика диалога. Роли — как в Messages API Anthropic.

    `images` — картинки перед текстом реплики (spec 0049 Р-13). Из сериализации
    исключены: у запроса без картинок хэш промпта прежний (`service._request_payload`).
    """

    role: Literal["user", "assistant"]
    content: str
    images: list[LlmImage] = Field(default_factory=list, exclude=True)


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
    # Имя инструмента, который модель ОБЯЗАНА вызвать (`tool_choice: {type: "tool"}`
    # Anthropic). None — прежнее поведение (auto). Нужен структурным решениям,
    # где свободный текст недопустим (гейт P-9, Task 0017.1).
    forced_tool: str | None = None
    # Назначение вызова — чья модель и чей дневной бюджет (ADR-024 §1). В
    # prompt_hash не входит: хэш — версия того, что видела модель, а модель и
    # так пишется отдельной колонкой журнала.
    purpose: LlmPurpose = "guest"


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


class EmbeddingResponse(BaseModel):
    """Векторы текстов в порядке входа + учёт вызова (строка `LlmCallLog`, ADR-024 §4)."""

    call_id: uuid.UUID
    vectors: list[list[float]]
    model: str
    input_tokens: int
    cost_usd: Decimal
    latency_ms: int


class LlmTextDelta(BaseModel):
    """Очередной кусок текста потока (ADR-024 §5)."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class LlmStreamCompleted(BaseModel):
    """Последнее событие потока: весь ответ и его учёт, уже записанный в журнал."""

    type: Literal["completed"] = "completed"
    response: LlmResponse


LlmStreamEvent = LlmTextDelta | LlmStreamCompleted
