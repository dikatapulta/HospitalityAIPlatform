"""Ответ модели целиком — для текста, который прочтёт человек (#236, аудит 05.08 В-6).

`complete()` отдаёт ответ провайдера как есть, и оборванный лимитом ответ
выглядит обычным успехом: гость получает реплику на полуслове, а вызов
инструмента — правдоподобно неполные аргументы (по ним обрыв не узнать, только
по причине остановки). `complete_whole` — тот же вызов с проверкой причины:

- обрыв лимитом (`LlmResponse.truncated`) — один повтор с удвоенным
  `max_tokens` в остаток того же потолка (`Deadline`, spec 0055). Учёт ответа —
  сумма обоих вызовов: его складывает лимит чата (spec 0023 §5); строка журнала
  у каждого вызова своя;
- снова обрыв или отказ модели (`LlmResponse.refused`) — `AppError` ERR-AI-007.
  Неполный ответ человеку не отдаётся; деградацию решает вызывающий, как при
  ERR-AI-001/003. Отказ не повторяется: тот же запрос откажут снова.
"""

from __future__ import annotations

from typing import Final

from hospitality.ai.gateway.deadline import Deadline
from hospitality.ai.gateway.provider import LlmProvider
from hospitality.ai.gateway.schemas import LlmRequest, LlmResponse
from hospitality.ai.gateway.service import complete
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AI_INCOMPLETE_ANSWER: Final = "ERR-AI-007"

# Во сколько раз повтор поднимает лимит выхода. Типовой ход гостя тратит ~300
# токенов из 1024 (резерв, service.py); в лимит упирается редкий ход — несколько
# заявок одним сообщением (spec 0078) или длинный ответ, — и вдвое ему хватает.
# Больше — дороже резерв повтора, а модели, зациклившейся на тексте, не поможет
# никакой лимит.
RETRY_MAX_TOKENS_FACTOR: Final = 2


async def complete_whole(
    request: LlmRequest,
    *,
    provider: LlmProvider | None = None,
    deadline: Deadline | None = None,
) -> LlmResponse:
    """`complete()`, но ответ либо целиком, либо ERR-AI-007 (контракт — докстринг модуля)."""
    response = await complete(request, provider=provider, deadline=deadline)
    if response.truncated:
        logger.warning(
            "llm_answer_truncated_retry",
            purpose=request.purpose,
            stop_reason=response.stop_reason,
            max_tokens=request.max_tokens,
            output_tokens=response.output_tokens,
        )
        retry = request.model_copy(
            update={"max_tokens": request.max_tokens * RETRY_MAX_TOKENS_FACTOR}
        )
        second = await complete(retry, provider=provider, deadline=deadline)
        response = second.model_copy(
            update={
                "input_tokens": response.input_tokens + second.input_tokens,
                "output_tokens": response.output_tokens + second.output_tokens,
                "cost_usd": response.cost_usd + second.cost_usd,
                "latency_ms": response.latency_ms + second.latency_ms,
            }
        )
    if response.truncated or response.refused:
        logger.warning(
            "llm_answer_incomplete",
            error_code=ERR_AI_INCOMPLETE_ANSWER,
            purpose=request.purpose,
            stop_reason=response.stop_reason,
            cost_usd=str(response.cost_usd),
        )
        raise AppError(
            code=ERR_AI_INCOMPLETE_ANSWER,
            message="LLM answer is incomplete",
            status_code=502,
        )
    return response
