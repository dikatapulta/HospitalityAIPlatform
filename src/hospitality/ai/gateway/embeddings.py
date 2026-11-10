"""Эмбеддинги через шлюз (ADR-024 §4): поиск статьи кодекса по смыслу.

Порядок тот же, что у `complete()`, и шаги — его же функции (`service.py`):
резерв по оценке входа «символов / 2» (ADR-017) → бюджет назначения → попытки с
ретраем по таймауту и временному отказу (`retry.py`) → стоимость → строка
журнала (`output_tokens = 0`, `prompt_hash` — sha256 сериализованных текстов) →
снятие резерва. Один вызов
шлюза — одна строка журнала: пачки по лимитам провайдера делит адаптер. Отмена
задачи — строка `aborted` со стоимостью резерва и снятие резерва под щитом (#531).

Модель одна на деплоймент — `LLM_EMBEDDING_MODEL`, а бюджет — назначения
вызова: эмбеддинги наставника платит бюджет наставника. Назначение без модели
чата (`LLM_MODEL_MENTOR` пуст) — ERR-AI-006, как у `complete()`.

Сбой посреди пачек (первая оплачена, вторая упала) адаптер отдаёт оплаченной
ошибкой с токенами оплаченных пачек, и строка `error` несёт их стоимость (#426 §1);
повтора нет — он оплатил бы первые пачки ещё раз. Весь кодекс пилота — одна пачка
(`openai_provider._EMBED_BATCH_CHARS`), и цена эмбеддингов — доли цента.

Отдельным файлом: `service.py` и без эмбеддингов за границей R-3.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from decimal import Decimal

from hospitality.ai.gateway.cancellation import finish_despite_cancel
from hospitality.ai.gateway.models import LlmCallStatus
from hospitality.ai.gateway.price_list import EMBEDDING_DIMENSIONS, EMBEDDING_PRICING_USD_PER_MTOK
from hospitality.ai.gateway.provider import (
    EmbeddingProvider,
    LlmProviderError,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.providers import embedding_provider_for_model
from hospitality.ai.gateway.purposes import LlmPurpose, require_purpose
from hospitality.ai.gateway.retry import _sleep_before_retry, _transient_pause_seconds
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
    log_aborted_call,
)
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

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

    async def log(
        status: LlmCallStatus, input_tokens: int = 0, cost_usd: Decimal = Decimal(0)
    ) -> None:
        """Строка журнала неуспешного исхода; токены — только оплаченных пачек (#426 §1)."""
        await _log_call(
            provider=provider.name,
            model=model,
            purpose=purpose,
            prompt_hash=prompt_hash,
            status=status,
            input_tokens=input_tokens,
            cost_usd=cost_usd,
            latency_ms=_elapsed_ms(started_at),
        )

    reserve_usd = _reserve_amount(
        chars=len(payload), input_price=price, output_tokens=0, output_price=Decimal(0)
    )
    tenant_id = current_tenant_id()
    reservation_id = await _reserve_budget(reserve_usd, settings, purpose)
    outcome_logged = False
    provider_called = False
    try:
        try:
            await _ensure_tenant_budget(config.daily_budget_usd, purpose)
        except AppError:
            await log(LlmCallStatus.BUDGET_EXCEEDED)
            outcome_logged = True
            raise

        for attempt in range(1, settings.llm_max_attempts + 1):
            provider_called = True
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
                    outcome_logged = True
                    raise AppError(
                        code=ERR_AI_PROVIDER_TIMEOUT,
                        message="LLM provider did not respond in time",
                        status_code=503,
                    ) from None
                await _sleep_before_retry(attempt, settings)
            except LlmProviderError as error:
                pause = _transient_pause_seconds(error, attempt, settings)
                if pause is not None:
                    logger.warning(
                        "llm_call_retry",
                        provider=provider.name,
                        purpose=purpose,
                        error_kind=error.kind,
                        attempt=attempt,
                        max_attempts=settings.llm_max_attempts,
                    )
                    await asyncio.sleep(pause)
                    continue
                paid_tokens = error.input_tokens or 0
                if not error.billed:
                    cost_usd = Decimal(0)
                elif error.input_tokens is None:
                    cost_usd = reserve_usd
                else:
                    cost_usd = Decimal(paid_tokens) * price / _TOKENS_PER_MTOK
                await log(LlmCallStatus.ERROR, paid_tokens, cost_usd)
                outcome_logged = True
                logger.warning(
                    "llm_call_failed",
                    provider=provider.name,
                    purpose=purpose,
                    error_kind=error.kind,
                    attempt=attempt,
                    billed=error.billed,
                    cost_usd=str(cost_usd),
                    error=str(error),
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
        outcome_logged = True
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
    except asyncio.CancelledError:
        if not outcome_logged:
            # Как у `complete()` (#531): провайдер мог уже считать — стоимость резерва.
            await log_aborted_call(
                provider=provider.name,
                model=model,
                purpose=purpose,
                prompt_hash=prompt_hash,
                cost_usd=reserve_usd if provider_called else Decimal(0),
                started_at=started_at,
                tenant_id=tenant_id,
            )
        raise
    finally:
        # Как у `complete()`: резерв снимается ПОСЛЕ записи исхода, под щитом от отмены.
        await finish_despite_cancel(
            _release_budget_reservation(reservation_id), tenant_id=tenant_id
        )


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
