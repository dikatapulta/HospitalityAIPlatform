"""Повторы шлюза: что повторяется и сколько ждать перед повтором (ADR-017, #409).

Повторяется таймаут и временный отказ провайдера (`LlmProviderTransientError`:
обрыв соединения, 429 с `retry-after`, 5xx и 529); остальные ошибки — сразу
ERR-AI-003. Попыток — `LLM_MAX_ATTEMPTS` на всё вместе, пауза — одна формула.
Потолок паузы — `LLM_TIMEOUT_SECONDS` и для расчётной, и для `retry-after`:
на него опирается срок аренды резерва (`service._reservation_lease_seconds`).

Отдельным файлом: `service.py` и без этого за границей R-3; одни и те же правила
у `complete()`, потока и эмбеддингов.
"""

from __future__ import annotations

import asyncio
import random

from hospitality.ai.gateway.deadline import Deadline
from hospitality.ai.gateway.provider import LlmProviderError, LlmProviderTransientError
from hospitality.shared.config import Settings


def _retry_delay_seconds(attempt: int, settings: Settings) -> float:
    """Верхняя граница паузы после `attempt`-й попытки (формула канона ADR-009).

    `база × 2^(попытка−1)` — та же экспонента, что у доставки outbox; потолком
    служит `LLM_TIMEOUT_SECONDS`, а не отдельная настройка: ждать дольше одной
    попытки на ходе гостя, который ждёт ответа прямо сейчас, бессмысленно.
    """
    delay = settings.llm_retry_backoff_base_seconds * float(2 ** (attempt - 1))
    return min(delay, settings.llm_timeout_seconds)


def _retry_pause_seconds(attempt: int, settings: Settings) -> float:
    """Пауза перед следующей попыткой — с разбросом (jitter).

    ADR-009 разброс не делал осознанно: воркер один, и синхронизироваться его
    повторам не с чем. У шлюза наоборот — вызывающих столько, сколько гостей
    пишет прямо сейчас, и авария провайдера обрывает их таймаутом почти
    одновременно. Фиксированная пауза собрала бы все повторы в одну волну по
    и без того лежащему провайдеру — ровно то, против чего пауза и заводится.
    Разброс половинный: пауза остаётся в [0.5×, 1×] от расчётной, то есть не
    исчезает и не растягивает ожидание гостя сверх посчитанного.
    """
    return _retry_delay_seconds(attempt, settings) * (0.5 + random.random() / 2)  # noqa: S311


async def _sleep_before_retry(attempt: int, settings: Settings) -> None:
    await asyncio.sleep(_retry_pause_seconds(attempt, settings))


def _transient_pause_seconds(
    error: LlmProviderError,
    attempt: int,
    settings: Settings,
    deadline: Deadline | None = None,
) -> float | None:
    """Пауза перед повтором после `error` на попытке `attempt` или `None` — не повторять.

    Не повторяются: постоянная ошибка (ключ, баланс, сам запрос), последняя
    попытка, `retry-after` длиннее одной попытки (потолок паузы ADR-017) и пауза,
    которая не помещается в потолок вызывающего (spec 0055). `retry-after` —
    нижняя граница паузы: раньше провайдер не примет, а расчётная пауза с
    разбросом остаётся, если она длиннее.
    """
    if not isinstance(error, LlmProviderTransientError) or attempt >= settings.llm_max_attempts:
        return None
    pause = _retry_pause_seconds(attempt, settings)
    if error.retry_after_seconds is not None:
        if error.retry_after_seconds > settings.llm_timeout_seconds:
            return None
        pause = max(pause, error.retry_after_seconds)
    if deadline is not None and deadline.remaining_seconds() <= pause:
        return None
    return pause
