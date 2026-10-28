"""Эмбеддинги через шлюз (ADR-024 §4): поиск статьи кодекса по смыслу.

Порядок тот же, что у `complete()`, и шаги — его же функции (`service.py`):
резерв по оценке входа «символов / 2» (ADR-017) → бюджет назначения → попытки с
ретраем только по таймауту → стоимость → строка журнала (`output_tokens = 0`,
`prompt_hash` — sha256 сериализованных текстов) → снятие резерва. Один вызов
шлюза — одна строка журнала: пачки по лимитам провайдера делит адаптер.

Модель одна на деплоймент — `LLM_EMBEDDING_MODEL`, а бюджет — назначения
вызова: эмбеддинги наставника платит бюджет наставника. Назначение без модели
чата (`LLM_MODEL_MENTOR` пуст) — ERR-AI-006, как у `complete()`.

Ограничение: ошибка посреди пачек (первая оплачена, вторая упала) пишется
стоимостью 0 — остаток дефекта #426 §1. Весь кодекс пилота — одна пачка
(`openai_provider._EMBED_BATCH_CHARS`), и цена эмбеддингов — доли цента.

Отдельным файлом: `service.py` и без эмбеддингов за границей R-3.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal

from hospitality.ai.gateway.models import LlmCallStatus
from hospitality.ai.gateway.price_list import EMBEDDING_DIMENSIONS, EMBEDDING_PRICING_USD_PER_MTOK
from hospitality.ai.gateway.provider import (
    EmbeddingProvider,
    LlmProviderError,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.providers import embedding_provider_for_model
from hospitality.ai.gateway.purposes import LlmPurpose, require_purpose
from hospitality.ai.gateway.schemas import EmbeddingResponse
from hospitality.ai.gateway.service import (
    _TOKENS_PER_MTOK,
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PROVIDER_TIMEOUT,
    _elapsed_ms,
    _ensure_tenant_budget,
    _log_call,
    _release_budget_reservation,
    _reserve_amount,
    _reserve_budget,
    _sleep_before_retry,
)
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def embed(
    texts: list[str], *, purpose: LlmPurpose, provider: EmbeddingProvider | None = None
) -> EmbeddingResponse:
    """Векторы текстов в порядке входа от имени текущего тенанта (P-4).

    Пустой список или пустой текст — `ValueError`: провайдер отверг бы их
    оплаченным по сети отказом, а это ошибка вызывающего кода.
    """
    if not texts or any(not text for text in texts):
        raise ValueError("embed: нужен непустой список непустых текстов")
    settings = get_settings()
    config = require_purpose(purpose, settings)
    model = settings.llm_embedding_model
    if provider is None:
        provider = embedding_provider_for_model(model)
    payload = json.dumps(texts, ensure_ascii=False)
    prompt_hash = hashlib.sha256(payload.encode()).hexdigest()
    price = _embedding_price(model)

    started_at = time.monotonic()

    async def log(status: LlmCallStatus) -> None:
        """Строка журнала неуспешного исхода — без токенов и со стоимостью 0."""
        await _log_call(
            provider=provider.name,
            model=model,
            purpose=purpose,
            prompt_hash=prompt_hash,
            status=status,
            latency_ms=_elapsed_ms(started_at),
        )

    reserve_usd = _reserve_amount(
        chars=len(payload), input_price=price, output_tokens=0, output_price=Decimal(0)
    )
    reservation_id = await _reserve_budget(reserve_usd, settings, purpose)
    try:
        try:
            await _ensure_tenant_budget(config.daily_budget_usd, purpose)
        except AppError:
            await log(LlmCallStatus.BUDGET_EXCEEDED)
            raise

        for attempt in range(1, settings.llm_max_attempts + 1):
            try:
                result = await provider.embed(texts)
                break
            except LlmProviderTimeoutError:
                logger.warning(
                    "llm_call_timeout",
                    provider=provider.name,
                    purpose=purpose,
                    attempt=attempt,
                    max_attempts=settings.llm_max_attempts,
                )
                if attempt == settings.llm_max_attempts:
                    await log(LlmCallStatus.TIMEOUT)
                    raise AppError(
                        code=ERR_AI_PROVIDER_TIMEOUT,
                        message="LLM provider did not respond in time",
                        status_code=503,
                    ) from None
                await _sleep_before_retry(attempt, settings)
            except LlmProviderError as error:
                await log(LlmCallStatus.ERROR)
                logger.warning(
                    "llm_call_failed", provider=provider.name, purpose=purpose, error=str(error)
                )
                raise _provider_error() from error
        else:  # pragma: no cover — `llm_max_attempts >= 1` держит Field(ge=1)
            raise AssertionError("embed: ни одной попытки")

        latency_ms = _elapsed_ms(started_at)
        cost_usd = Decimal(result.input_tokens) * price / _TOKENS_PER_MTOK
        malformed = len(result.vectors) != len(texts) or any(
            len(vector) != EMBEDDING_DIMENSIONS for vector in result.vectors
        )
        # Ответ оплачен в любом случае — строка со стоимостью пишется до проверки
        # формы, чтобы дневной бюджет видел и негодный ответ (#426 §1).
        call_id = await _log_call(
            provider=provider.name,
            model=result.model,
            purpose=purpose,
            prompt_hash=prompt_hash,
            status=LlmCallStatus.ERROR if malformed else LlmCallStatus.OK,
            input_tokens=result.input_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        if malformed:
            logger.warning(
                "llm_embedding_malformed",
                provider=provider.name,
                expected=len(texts),
                received=len(result.vectors),
                dimensions=EMBEDDING_DIMENSIONS,
            )
            raise _provider_error()
        logger.info(
            "llm_embed",
            provider=provider.name,
            model=result.model,
            purpose=purpose,
            texts=len(texts),
            input_tokens=result.input_tokens,
            cost_usd=str(cost_usd),
            latency_ms=latency_ms,
        )
        return EmbeddingResponse(
            call_id=call_id,
            vectors=result.vectors,
            model=result.model,
            input_tokens=result.input_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
    finally:
        # Как у `complete()`: резерв снимается ПОСЛЕ записи исхода.
        await _release_budget_reservation(reservation_id)


def _embedding_price(model: str) -> Decimal:
    """Цена модели эмбеддингов; вне группы прайс-листа — самая дорогая строка группы.

    Боевой процесс с такой моделью не поднимется (`validate_configured_model`);
    ветка — для тестов и разовых скриптов, как `service._reserve_pricing`.
    """
    price = EMBEDDING_PRICING_USD_PER_MTOK.get(model)
    return price if price is not None else max(EMBEDDING_PRICING_USD_PER_MTOK.values())


def _provider_error() -> AppError:
    return AppError(
        code=ERR_AI_PROVIDER_ERROR, message="LLM provider request failed", status_code=502
    )
