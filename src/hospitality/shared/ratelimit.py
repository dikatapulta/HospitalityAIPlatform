"""CANONICAL: счётчик rate-limit в Redis (issue #41, spec 0023, FOUNDATION §6, P-12).

Единственный канонический паттерн работы с Redis в бизнес-коде и единственный
канон rate-limit'а платформы. Fixed-window: ключ содержит номер окна
(`bucket = time // window`), поэтому смена окна сама «обнуляет» счёт, а гонка
между INCR и EXPIRE безвредна (EXPIRE — только гигиена мусорных ключей).
Известный компромисс fixed-window — всплеск до 2N на границе окон — принят
осознанно (spec 0023): точность не цель, потолок расхода держит бюджет
тенанта (ERR-AI-002).

Fail-open: недоступный Redis разрешает вызов (`allowed=True, available=False`)
с WARNING `rate_limit_backend_unavailable`. Отказ в обслуживании хуже
перерасхода: fail-closed превратил бы упавший Redis в отключение диалога всем
гостям всех тенантов, а перерасход и так ограничен сверху дневным бюджетом
LLM (ERR-AI-002). Обоснование — spec 0023, раздел 2.

Счётчик стоит на горячем пути диалога, поэтому таймаут операций короткий
(1 секунда) и ретраев нет — при недоступном Redis гость не ждёт.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

import redis.asyncio as redis

from hospitality.shared.config import get_settings
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Короткий таймаут: лимитер не имеет права заметно задерживать ответ гостю,
# fail-open срабатывает не позже чем через секунду.
_OPERATION_TIMEOUT_SECONDS = 1.0


class RateLimitRedis(Protocol):
    """Подмножество `redis.asyncio.Redis`, нужное счётчику.

    Протокол — точка подмены в тестах (тот же приём, что порт `sender`/`provider`
    в канале): CI гоняет юнит-тесты без живого Redis.
    """

    def incr(self, name: str) -> Any: ...

    def expire(self, name: str, time: int) -> Any: ...


@dataclass(frozen=True)
class RateLimitDecision:
    """Итог одного `consume_rate_limit`: пропускать ли и что случилось со счётом."""

    allowed: bool
    count: int  # номер этого вызова в окне; 0 — Redis недоступен (fail-open)
    limit: int
    available: bool  # False — решение принято fail-open'ом, а не счётом

    @property
    def first_rejection(self) -> bool:
        """Первое превышение в окне — момент для единственного ответа-отказа.

        Дальнейшие отклонения в том же окне вызывающий код гасит молча, иначе
        спам в N сообщений породил бы N ответов-отказов (spec 0023, раздел 3).
        """
        return self.count == self.limit + 1


def create_redis_client() -> redis.Redis:
    """Клиент Redis из настроек окружения — на один вызов, как `health.check_redis`.

    Намеренно НЕ синглтон (в отличие от `get_engine`): пул redis-py привязан к
    event loop'у, где создан, — закэшированный клиент ломается при нескольких
    loop'ах на процесс (каждый тест pytest-asyncio живёт в своём). Цена —
    TCP-connect на вызов; при частоте сообщений гостя это незаметно, а первый
    же более горячий потребитель заберёт себе loop-безопасный пул отдельной
    задачей.
    """
    return redis.from_url(
        get_settings().redis_dsn,
        socket_connect_timeout=_OPERATION_TIMEOUT_SECONDS,
        socket_timeout=_OPERATION_TIMEOUT_SECONDS,
    )


async def consume_rate_limit(
    scope: str,
    key: str,
    *,
    limit: int,
    window_seconds: int,
    client: RateLimitRedis | None = None,
    now: float | None = None,
) -> RateLimitDecision:
    """Учесть одно событие и решить, укладывается ли оно в лимит окна (канон, P-12).

    `scope` — имя лимита (часть ключа, логов и метрик), `key` — чей лимит;
    для тенантных данных ключ обязан начинаться с tenant_id (P-4: у Redis нет
    RLS, изоляция тенантов держится только дисциплиной ключей). `client` и
    `now` переопределяются только в тестах.
    """
    own_client: redis.Redis | None = None
    if client is None:
        client = own_client = create_redis_client()
    bucket = int((now if now is not None else time.time()) // window_seconds)
    redis_key = f"ratelimit:{scope}:{window_seconds}:{key}:{bucket}"
    try:
        count = int(await client.incr(redis_key))
        if count == 1:
            # Гигиена: ключ прошлых окон никому не нужен. Потерянный EXPIRE
            # (упали между командами) не влияет на счёт — окно живёт в bucket.
            await client.expire(redis_key, window_seconds * 2)
    except (OSError, redis.RedisError, TimeoutError):
        logger.warning("rate_limit_backend_unavailable", scope=scope, exc_info=True)
        return RateLimitDecision(allowed=True, count=0, limit=limit, available=False)
    finally:
        if own_client is not None:
            with suppress(Exception):
                # Закрытие соединения не имеет права ломать ход гостя.
                await own_client.aclose()
    return RateLimitDecision(allowed=count <= limit, count=count, limit=limit, available=True)
