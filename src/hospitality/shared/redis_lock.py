"""Замок по ключу в Redis: дела одного ключа идут по очереди (issue #91).

Первый потребитель — ход гостя (`channels/common/guest_turn.py`): два быстрых
сообщения одного диалога шли двумя параллельными ходами, и второй не видел ни
ответа первого, ни его ожидания подтверждения (P-9).

Почему Redis. Замок в памяти процесса не видит соседний процесс: в CMD образа
`--workers 2`. Advisory-замок Postgres держал бы соединение пула всё время
ожидания модели, до 25 с на ход (spec 0055): десяток гостей разом занял бы пул,
который нужен тем же ходам.

Устройство: `SET key token NX PX ttl`, ждущий повторяет попытку каждые
`_POLL_SECONDS`. Снимает замок только владелец: Lua сравнивает токен и удаляет.
Без сравнения дело, пережившее TTL, сняло бы уже чужой замок. TTL страхует от
упавшего процесса: замок умершего владельца исчезнет сам.

Fail-open, как канон rate-limit (`ratelimit.py`): Redis недоступен или ожидание
вышло за `wait_seconds` — дело идёт без замка, с WARNING. Отказать гостю в
ответе из-за упавшего Redis хуже, чем редкая гонка, которая и так была до замка.
Клиент берётся из канона подключения `ratelimit.create_redis_client` — через
модуль, чтобы его подмена в тестах доставала и замок.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Final, Protocol

import redis.asyncio as redis

from hospitality.shared import ratelimit
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Шаг ожидания: ход гостя длится секунды, 0,1 с задержки после чужого хода
# незаметны, а 10 запросов SET в секунду Redis не замечает.
_POLL_SECONDS: Final = 0.1

# Снять замок, только если он всё ещё наш: GET и DEL одной атомарной операцией.
_RELEASE_SCRIPT: Final = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)

_BACKEND_ERRORS: Final = (OSError, redis.RedisError, TimeoutError)


class LockRedis(Protocol):
    """Подмножество `redis.asyncio.Redis`, нужное замку; точка подмены в тестах."""

    def set(self, name: str, value: str, *, nx: bool, px: int) -> Any: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any: ...


@asynccontextmanager
async def hold_redis_lock(
    scope: str,
    key: str,
    *,
    ttl_seconds: float,
    wait_seconds: float,
    client: LockRedis | None = None,
) -> AsyncIterator[bool]:
    """Держать замок `scope`+`key` на время блока; ждать чужой не дольше `wait_seconds`.

    Отдаёт True — замок наш, False — блок идёт без замка (fail-open, причина
    в WARNING). `key` тенантных дел обязан начинаться с tenant_id (P-4: у Redis
    нет RLS). `ttl_seconds` — с запасом над самым долгим блоком: блок, переживший
    TTL, уже не защищён, и его конец пишет WARNING `redis_lock_expired`.
    `client` подменяется только в тестах.
    """
    own_client: redis.Redis | None = None
    if client is None:
        client = own_client = ratelimit.create_redis_client()
    redis_key = f"lock:{scope}:{key}"
    token = secrets.token_hex(16)
    try:
        held = await _acquire(
            client,
            redis_key,
            token,
            scope=scope,
            ttl_ms=int(ttl_seconds * 1000),
            wait_seconds=wait_seconds,
        )
        try:
            yield held
        finally:
            if held:
                await _release(client, redis_key, token, scope=scope, ttl_seconds=ttl_seconds)
    finally:
        if own_client is not None:
            with suppress(Exception):
                # Закрытие соединения не имеет права ломать само дело.
                await own_client.aclose()


async def _acquire(
    client: LockRedis,
    redis_key: str,
    token: str,
    *,
    scope: str,
    ttl_ms: int,
    wait_seconds: float,
) -> bool:
    started = time.monotonic()
    waited = False
    while True:
        try:
            if await client.set(redis_key, token, nx=True, px=ttl_ms):
                if waited:
                    # Замок сработал: дело ждало чужое — видно, как часто и сколько.
                    logger.info(
                        "redis_lock_waited",
                        scope=scope,
                        waited_seconds=round(time.monotonic() - started, 2),
                    )
                return True
        except _BACKEND_ERRORS:
            logger.warning("redis_lock_backend_unavailable", scope=scope, exc_info=True)
            return False
        if time.monotonic() - started >= wait_seconds:
            logger.warning("redis_lock_wait_exceeded", scope=scope, wait_seconds=wait_seconds)
            return False
        waited = True
        await asyncio.sleep(_POLL_SECONDS)


async def _release(
    client: LockRedis, redis_key: str, token: str, *, scope: str, ttl_seconds: float
) -> None:
    try:
        released = await client.eval(_RELEASE_SCRIPT, 1, redis_key, token)
    except _BACKEND_ERRORS:
        # Дело сделано; замок снимет TTL — следующий подождёт, но не дольше него.
        logger.warning("redis_lock_release_failed", scope=scope, exc_info=True)
        return
    if not released:
        logger.warning("redis_lock_expired", scope=scope, ttl_seconds=ttl_seconds)
