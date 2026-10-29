"""Поток ответа модели через шлюз (ADR-024 §5): первое слово раньше.

Порядок тот же, что у `complete()` (`service.py`), и шаги — его же функции:
резерв → бюджет назначения → попытки → стоимость → журнал → снятие резерва.
Отличия потока:

- **Ретрай по таймауту — только до первого куска.** Кусок ушёл человеку —
  повтор начал бы ответ заново; ошибка после него — ERR-AI-003 и строка `error`
  со стоимостью резерва: провайдер уже писал ответ, и вызов оплачен.
- **Обрыв.** Потребитель закрыл поток (`aclose`, ушёл клиент) или задачу
  отменили — строка `aborted` со стоимостью резерва. Резерв — верхняя граница
  (выход ограничен `max_tokens`, вход оценён с запасом), расход не занижается.
  Запись идёт под защитой от отмены (`_finish_despite_cancel`).
- **Нет `usage`.** Поток кончился без числа токенов — тоже стоимость резерва,
  а не ноль: новый путь не наследует дефект #426 §1.
- **Инструментов и картинок нет** (`ValueError`): наставнику они не нужны, а разбор
  вызова инструмента по кускам — отдельная работа.

Отдельным файлом: `service.py` и без потока за границей R-3.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator, Coroutine
from contextlib import aclosing
from decimal import Decimal
from typing import Any

import anyio

from hospitality.ai.gateway.models import LlmCallStatus
from hospitality.ai.gateway.provider import (
    LlmProviderError,
    LlmProviderResult,
    LlmProviderStreamEnd,
    LlmProviderTimeoutError,
    LlmStreamingProvider,
)
from hospitality.ai.gateway.providers import streaming_provider_for_model
from hospitality.ai.gateway.purposes import require_purpose
from hospitality.ai.gateway.schemas import (
    LlmRequest,
    LlmResponse,
    LlmStreamCompleted,
    LlmStreamEvent,
    LlmTextDelta,
)
from hospitality.ai.gateway.service import (
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PROVIDER_TIMEOUT,
    _compute_cost,
    _elapsed_ms,
    _ensure_tenant_budget,
    _log_call,
    _release_budget_reservation,
    _reserve_budget,
    _reserve_usd,
    _sleep_before_retry,
    compute_prompt_hash,
)
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id, tenant_context

logger = get_logger(module=__name__)


async def stream(
    request: LlmRequest, *, provider: LlmStreamingProvider | None = None
) -> AsyncGenerator[LlmStreamEvent, None]:
    """Поток ответа от имени текущего тенанта: `LlmTextDelta`…, затем `LlmStreamCompleted`.

    Зовётся внутри `tenant_context(...)` (P-4). Потребитель закрывает поток через
    `contextlib.aclosing` — тогда обрыв записывается сразу, а не сборщиком мусора.
    Ошибки — те же коды, что у `complete()`, плюс ERR-AI-006 для выключенного
    назначения; приходят при первом шаге итерации или посреди потока.
    """
    if request.tools or request.forced_tool is not None:
        raise ValueError("инструменты в потоке шлюза запрещены (ADR-024 §5)")
    if any(message.images for message in request.messages):
        raise ValueError("картинки в потоке шлюза запрещены (spec 0049 Р-13)")
    settings = get_settings()
    purpose = request.purpose
    config = require_purpose(purpose, settings)
    if provider is None:
        provider = streaming_provider_for_model(config.model)
    tenant_id = current_tenant_id()
    prompt_hash = compute_prompt_hash(request)
    reserve_usd = _reserve_usd(request)
    started_at = time.monotonic()

    async def log(status: LlmCallStatus, cost_usd: Decimal = Decimal(0)) -> uuid.UUID:
        """Строка журнала неуспешного исхода — без токенов: их провайдер не сообщил."""
        return await _log_call(
            provider=provider.name,
            model=config.model,
            purpose=purpose,
            prompt_hash=prompt_hash,
            status=status,
            cost_usd=cost_usd,
            latency_ms=_elapsed_ms(started_at),
        )

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

        chunks: list[str] = []
        end: LlmProviderStreamEnd | None = None
        for attempt in range(1, settings.llm_max_attempts + 1):
            provider_called = True
            try:
                async with aclosing(provider.stream(request)) as events:
                    async for item in events:
                        if isinstance(item, LlmProviderStreamEnd):
                            end = item
                            break
                        chunks.append(item)
                        yield LlmTextDelta(text=item)
                break
            except LlmProviderTimeoutError:
                logger.warning(
                    "llm_call_timeout",
                    provider=provider.name,
                    purpose=purpose,
                    attempt=attempt,
                    max_attempts=settings.llm_max_attempts,
                    streamed=bool(chunks),
                )
                if chunks:
                    await log(LlmCallStatus.ERROR, reserve_usd)
                    outcome_logged = True
                    raise _provider_error() from None
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
                # До первого куска провайдер ответил ошибкой — платить не за что;
                # после — ответ уже писался, и расход — верхняя граница резерва.
                await log(LlmCallStatus.ERROR, reserve_usd if chunks else Decimal(0))
                outcome_logged = True
                logger.warning(
                    "llm_call_failed", provider=provider.name, purpose=purpose, error=str(error)
                )
                raise _provider_error() from error

        text = "".join(chunks)
        if end is None or end.input_tokens is None or end.output_tokens is None:
            logger.warning("llm_stream_usage_missing", provider=provider.name, purpose=purpose)
            model, input_tokens, output_tokens, cost_usd = config.model, 0, 0, reserve_usd
        else:
            model, input_tokens, output_tokens = end.model, end.input_tokens, end.output_tokens
            cost_usd = _compute_cost(
                LlmProviderResult(
                    text=text, model=model, input_tokens=input_tokens, output_tokens=output_tokens
                )
            )
        latency_ms = _elapsed_ms(started_at)
        call_id = await _log_call(
            provider=provider.name,
            model=model,
            purpose=purpose,
            prompt_hash=prompt_hash,
            status=LlmCallStatus.OK,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        outcome_logged = True
        logger.info(
            "llm_call",
            provider=provider.name,
            model=model,
            purpose=purpose,
            prompt_hash=prompt_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=str(cost_usd),
            latency_ms=latency_ms,
            streamed=True,
        )
        yield LlmStreamCompleted(
            response=LlmResponse(
                call_id=call_id,
                text=text,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                prompt_hash=prompt_hash,
                stop_reason=end.stop_reason if end is not None else None,
            )
        )
    except (GeneratorExit, asyncio.CancelledError):
        if not outcome_logged:
            cost_usd = reserve_usd if provider_called else Decimal(0)
            logger.warning("llm_stream_aborted", provider=provider.name, purpose=purpose)
            await _finish_despite_cancel(log(LlmCallStatus.ABORTED, cost_usd), tenant_id=tenant_id)
        raise
    finally:
        # Как у `complete()`: резерв снимается ПОСЛЕ записи исхода.
        await _finish_despite_cancel(
            _release_budget_reservation(reservation_id), tenant_id=tenant_id
        )


def _provider_error() -> AppError:
    return AppError(
        code=ERR_AI_PROVIDER_ERROR, message="LLM provider request failed", status_code=502
    )


async def _finish_despite_cancel(write: Coroutine[Any, Any, Any], *, tenant_id: uuid.UUID) -> None:
    """Довести запись до конца, даже если задачу отменяют.

    Отмена бывает двух видов, и защищаться приходится от обоих (проверено
    пробой на asyncio и anyio 4.15): anyio (Starlette, отмена scope'а)
    повторяет отмену на каждом `await` — от неё прикрывает щит `CancelScope`;
    обычный `task.cancel()` asyncio щит anyio пропускает — от него прикрывает
    отдельная задача и `asyncio.shield`, которых ждём до конца. Задача создаётся
    в контексте тенанта: поток, закрытый сборщиком мусора, финализируется в
    чужом контексте, а журнал — тенантная таблица (P-4).

    Отмена, пойманная во время ожидания, не теряется — после записи поднимается
    снова. Сбой самой записи — WARNING, а не исключение: он не вправе подменить
    собой отмену или исход вызова (незакрытый резерв истечёт по аренде, ADR-017).
    """
    with tenant_context(tenant_id):
        task = asyncio.ensure_future(write)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    if task.exception() is not None:
        logger.warning("llm_stream_outcome_write_failed", exc_info=task.exception())
    if cancelled:
        raise asyncio.CancelledError
