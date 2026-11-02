"""Health-эндпоинты (Task 0005, FOUNDATION §10.6).

`/health/live` — процесс жив, без обращения к зависимостям.
`/health/ready` — пингует Postgres и Redis и проверяет хранилище файлов
(spec 0047: health адаптера, FOUNDATION §8 правило 1), отдаёт 503 при отказе
любого из них, и называет версию кода — тег образа (#411): по нему видно, на каком
коммите стоит контур и насколько гостевая копия отстала от `main`. Postgres —
через пул приложения, а не новым соединением (#487): адрес открыт снаружи, и цикл
`curl` по нему исчерпывал бы `max_connections` базы. Проверки — это ожидаемый путь
диагностики, поэтому ошибки соединения намеренно превращаются в структурированный
статус, а не пробрасываются (R-8 касается непроверенных ошибок бизнес-логики, а не
диагностических пингов).
"""

from __future__ import annotations

import asyncio

import asyncpg
import redis.asyncio as redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from hospitality.shared.config import Settings, get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.ports.file_storage import file_storage_or_none

router = APIRouter(prefix="/health", tags=["health"])

_CONNECT_TIMEOUT_SECONDS = 2.0


async def check_postgres() -> bool:
    # Тот же пул и та же роль, что у запросов гостя: соединений не больше пула,
    # а «готов» значит «приложение получает соединение», а не «база где-то жива».
    try:
        async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS), platform_session_scope() as session:
            await session.execute(text("SELECT 1"))
    except (OSError, SQLAlchemyError, asyncpg.PostgresError, TimeoutError):
        return False
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


async def check_file_storage() -> bool:
    # Не положенный адаптер — ошибка сборки процесса: том файлов недоступен так же.
    storage = file_storage_or_none()
    if storage is None:
        return False
    try:
        return await storage.check()
    except Exception:
        return False


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(settings: Settings = Depends(get_settings)) -> JSONResponse:
    postgres_ok = await check_postgres()
    redis_ok = await check_redis(settings.redis_dsn)
    files_ok = await check_file_storage()
    healthy = postgres_ok and redis_ok and files_ok

    body = {
        "status": "ok" if healthy else "unavailable",
        "version": settings.app_release or "unknown",
        "checks": {
            "postgres": "ok" if postgres_ok else "error",
            "redis": "ok" if redis_ok else "error",
            "file_storage": "ok" if files_ok else "error",
        },
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
