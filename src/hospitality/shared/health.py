"""Health-эндпоинты (Task 0005, FOUNDATION §10.6).

`/health/live` — процесс жив, без обращения к зависимостям.
`/health/ready` — пингует Postgres и Redis, отдаёт 503 при недоступности любой
из них. Проверки зависимостей выше kernel'а (хранилище файлов за портом,
spec 0047 §6) подключает composition root через `register_readiness_check`:
kernel не знает ни `platform/`, ни адаптеров (R-5). Проверки — это ожидаемый
путь диагностики, поэтому ошибки соединения намеренно превращаются в
структурированный статус, а не пробрасываются (R-8 касается непроверенных
ошибок бизнес-логики, а не диагностических пингов).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import asyncpg
import redis.asyncio as redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from hospitality.shared.config import Settings, get_settings

router = APIRouter(prefix="/health", tags=["health"])

_CONNECT_TIMEOUT_SECONDS = 2.0

ReadinessCheck = Callable[[], Awaitable[bool]]
_readiness_checks: dict[str, ReadinessCheck] = {}


def register_readiness_check(name: str, check: ReadinessCheck) -> None:
    """Добавить проверку в `/health/ready`; повтор с тем же именем заменяет её."""
    _readiness_checks[name] = check


async def check_postgres(dsn: str) -> bool:
    try:
        connection = await asyncpg.connect(dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return False
    try:
        await connection.execute("SELECT 1")
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return False
    finally:
        await connection.close()
    return True


async def check_redis(url: str) -> bool:
    client = redis.from_url(
        url,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        return bool(await client.ping())
    except (OSError, redis.RedisError, TimeoutError):
        return False
    finally:
        await client.aclose()


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(settings: Settings = Depends(get_settings)) -> JSONResponse:
    results = {
        "postgres": await check_postgres(settings.postgres_dsn),
        "redis": await check_redis(settings.redis_dsn),
    }
    for name, check in _readiness_checks.items():
        try:
            results[name] = await check()
        except Exception:
            # Упавшая проверка — тот же диагностический ответ «error», что и
            # отказ соединения выше (докстринг модуля), а не 500 вместо 503.
            results[name] = False
    healthy = all(results.values())

    body = {
        "status": "ok" if healthy else "unavailable",
        "checks": {name: "ok" if ok else "error" for name, ok in results.items()},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
