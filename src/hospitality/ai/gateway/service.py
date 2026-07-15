"""Логика AI Gateway (Task 0014, FOUNDATION §7.2).

Единственный путь поговорить с LLM: `complete()` внутри `tenant_context(...)`
(P-4). Порядок вызова: бюджет тенанта → до `LLM_MAX_ATTEMPTS` попыток
провайдера (ретрай только по таймауту) → стоимость по прайс-листу → строка
`LlmCallLog` + событие `llm_call` в логах. Каждый исход — успех, исчерпанные
таймауты, ошибка провайдера — оставляет строку в журнале (DoD Task 0014).

Маршрутизации моделей нет (Non-Goal): одна модель `LLM_MODEL`. Ожидаемые
ошибки — `AppError` с кодами каталога (docs/runbooks/errors.md, R-8).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Final

import structlog
from sqlalchemy import func, select

from hospitality.ai.gateway.anthropic_provider import AnthropicProvider
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import (
    LlmProvider,
    LlmProviderError,
    LlmProviderResult,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmRequest, LlmResponse
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_llm_call

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AI_PROVIDER_TIMEOUT = "ERR-AI-001"
ERR_AI_BUDGET_EXCEEDED = "ERR-AI-002"
ERR_AI_PROVIDER_ERROR = "ERR-AI-003"

# Прайс-лист: $/1M токенов (input, output) по моделям. Единственное место
# истины для стоимости; модель вне прайс-листа — ошибка конфигурации,
# вызов падает до обращения к провайдеру (стоимость обязана считаться, §7.2).
# Кандидаты рантайма гостевого диалога (Task 0015) — Haiku 4.5 и Sonnet 5;
# финальный `LLM_MODEL` фиксируется bake-off'ом на 6 языках (spec 0015, §7.7).
# Sonnet 5 — стандартная цена $3/$15, НЕ интро $2/$10 (до 2026-08-31): COGS не
# должен занижаться молча после окончания интро-периода.
MODEL_PRICING_USD_PER_MTOK: Final[dict[str, tuple[Decimal, Decimal]]] = {
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

_TOKENS_PER_MTOK = Decimal(1_000_000)


def compute_prompt_hash(request: LlmRequest) -> str:
    """sha256 канонической сериализации запроса — «версия промпта» (§7.2).

    Сериализация детерминирована (порядок полей модели фиксирован), сам текст
    промпта в журнал не пишется (PII, §7.6) — только хэш.
    """
    payload = request.model_dump_json(include={"system", "messages", "tools", "forced_tool"})
    return hashlib.sha256(payload.encode()).hexdigest()


def build_anthropic_provider(model: str) -> AnthropicProvider:
    """Боевой Anthropic-адаптер под конкретную модель.

    Ключ и таймаут — из настроек, модель — параметром: композиции нужен
    провайдер под `LLM_MODEL`, а bake-off'у (§7.7, spec 0015) — под каждого
    кандидата (Haiku 4.5 / Sonnet 5) поочерёдно, через ту же единственную дверь.
    """
    settings = get_settings()
    return AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


@lru_cache
def get_default_provider() -> AnthropicProvider:
    """Боевой провайдер из настроек окружения — синглтон, создаётся лениво."""
    return build_anthropic_provider(get_settings().llm_model)


async def complete(request: LlmRequest, *, provider: LlmProvider | None = None) -> LlmResponse:
    """Выполнить вызов LLM от имени текущего тенанта (канонический путь, P-12).

    `provider` переопределяется только в тестах (MockLlmProvider) и композиции;
    бизнес-код зовёт без него — боевой Anthropic из настроек.
    """
    if provider is None:
        provider = get_default_provider()
    settings = get_settings()
    prompt_hash = compute_prompt_hash(request)

    await _ensure_tenant_budget(Decimal(str(settings.llm_tenant_daily_budget_usd)))

    max_attempts = settings.llm_max_attempts
    started_at = time.monotonic()
    result: LlmProviderResult | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = await provider.complete(request)
            break
        except LlmProviderTimeoutError:
            logger.warning(
                "llm_call_timeout",
                provider=provider.name,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            if attempt == max_attempts:
                await _log_call(
                    provider=provider.name,
                    model=settings.llm_model,
                    prompt_hash=prompt_hash,
                    status=LlmCallStatus.TIMEOUT,
                    latency_ms=_elapsed_ms(started_at),
                )
                raise AppError(
                    code=ERR_AI_PROVIDER_TIMEOUT,
                    message="LLM provider did not respond in time",
                    status_code=503,
                ) from None
        except LlmProviderError as error:
            await _log_call(
                provider=provider.name,
                model=settings.llm_model,
                prompt_hash=prompt_hash,
                status=LlmCallStatus.ERROR,
                latency_ms=_elapsed_ms(started_at),
            )
            logger.warning("llm_call_failed", provider=provider.name, error=str(error))
            raise AppError(
                code=ERR_AI_PROVIDER_ERROR,
                message="LLM provider request failed",
                status_code=502,
            ) from error
    assert result is not None  # цикл либо присвоил result, либо поднял AppError

    latency_ms = _elapsed_ms(started_at)
    cost_usd = _compute_cost(result)
    call_id = await _log_call(
        provider=provider.name,
        model=result.model,
        prompt_hash=prompt_hash,
        status=LlmCallStatus.OK,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    logger.info(
        "llm_call",
        provider=provider.name,
        model=result.model,
        prompt_hash=prompt_hash,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=str(cost_usd),
        latency_ms=latency_ms,
    )
    return LlmResponse(
        call_id=call_id,
        text=result.text,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        prompt_hash=prompt_hash,
        tool_calls=result.tool_calls,
        stop_reason=result.stop_reason,
    )


def _compute_cost(result: LlmProviderResult) -> Decimal:
    pricing = MODEL_PRICING_USD_PER_MTOK.get(result.model)
    if pricing is None:
        # Ошибка конфигурации/программиста, не бизнес-ошибка: наружу — 500.
        raise ValueError(
            f"model {result.model!r} is missing from MODEL_PRICING_USD_PER_MTOK: "
            "стоимость каждого вызова обязана считаться (FOUNDATION 7.2)"
        )
    input_price, output_price = pricing
    return (
        Decimal(result.input_tokens) * input_price + Decimal(result.output_tokens) * output_price
    ) / _TOKENS_PER_MTOK


async def _ensure_tenant_budget(daily_budget_usd: Decimal) -> None:
    """Простейший бюджет (Task 0014): сумма затрат тенанта за текущие
    UTC-сутки уже достигла лимита → отказ до обращения к провайдеру.
    Тенантная сессия — чужие затраты не видны и не считаются (RLS, P-4)."""
    day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as session:
        spent = await session.scalar(
            select(func.coalesce(func.sum(LlmCallLog.cost_usd), 0)).where(
                LlmCallLog.created_at >= day_start
            )
        )
    spent_usd = Decimal(spent if spent is not None else 0)
    if spent_usd >= daily_budget_usd:
        logger.warning(
            "llm_budget_exceeded",
            spent_usd=str(spent_usd),
            daily_budget_usd=str(daily_budget_usd),
        )
        raise AppError(
            code=ERR_AI_BUDGET_EXCEEDED,
            message="Tenant daily LLM budget is exhausted",
            status_code=429,
            # Бюджет дневной — раньше начала следующих UTC-суток повторять нет смысла.
            headers={
                "Retry-After": str(int((day_start + timedelta(days=1) - utc_now()).total_seconds()))
            },
        )


async def _log_call(
    *,
    provider: str,
    model: str,
    prompt_hash: str,
    status: LlmCallStatus,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal = Decimal(0),
    latency_ms: int = 0,
) -> uuid.UUID:
    """Записать вызов в `LlmCallLog` — отдельной короткой транзакцией
    (сетевые вызовы провайдера не живут внутри транзакции БД)."""
    # Метрики §10.7 — та же единая точка всех исходов, что и журнал (Task 0018).
    record_llm_call(
        model=model,
        status=status.value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    call = LlmCallLog(
        correlation_id=correlation_id if isinstance(correlation_id, str) else None,
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    async with session_scope() as session:
        session.add(call)
        await session.flush()
    return call.id


def _elapsed_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)
