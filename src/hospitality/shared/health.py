"""Health-эндпоинты (Task 0005, FOUNDATION §10.6).

`/health/live` — процесс жив, без обращения к зависимостям.
`/health/ready` — пингует Postgres и Redis, отдаёт 503 при недоступности любой
из них. Проверки — это ожидаемый путь диагностики, поэтому ошибки соединения
намеренно превращаются в структурированный статус, а не пробрасываются (R-8
касается непроверенных ошибок бизнес-логики, а не диагностических пингов).
"""

from __future__ import annotations

import asyncpg
import redis.asyncio as redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from hospitality.shared.config import Settings, get_settings

router = APIRouter(prefix="/health", tags=["health"])

_CONNECT_TIMEOUT_SECONDS = 2.0


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
    postgres_ok = await check_postgres(settings.postgres_dsn)
    redis_ok = await check_redis(settings.redis_dsn)
    healthy = postgres_ok and redis_ok

    body = {
        "status": "ok" if healthy else "unavailable",
        "checks": {
            "postgres": "ok" if postgres_ok else "error",
            "redis": "ok" if redis_ok else "error",
        },
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
