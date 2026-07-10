"""Общие фикстуры тестов (Task 0007/0008/0009).

Приложение собирается настоящим composition root (`create_app`) — так тесты
проверяют и сам канон, и его подключение. Служебные роуты добавляются поверх,
чтобы не тащить тестовый код в приложение.

Фикстуры БД: каждый тест получает СВЕЖУЮ временную базу — `migrated_database_name`
создаёт её и прогоняет `alembic upgrade head` (заодно проверяя применимость
миграций на чистый Postgres), `canonical_database` направляет канонический путь
(настройки → engine → session_scope) на неё. Нужен работающий Postgres
(`make dev`); без него DB-тесты пропускаются локально и падают в CI.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hospitality.app import create_app
from hospitality.shared.config import get_settings
from hospitality.shared.db import get_engine
from hospitality.shared.errors import AppError
from hospitality.shared.middleware import get_correlation_id

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_log_context() -> Iterator[None]:
    # Контекст логирования не должен утекать между тестами.
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Временная БД (Task 0008/0009)
# ---------------------------------------------------------------------------


async def _admin_execute(sql: str) -> None:
    settings = get_settings()
    connection = await asyncpg.connect(settings.postgres_dsn, timeout=2)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _postgres_available() -> bool:
    try:
        asyncio.run(_admin_execute("SELECT 1"))
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return False
    return True


@pytest.fixture
def migrated_database_name() -> Iterator[str]:
    """Чистая временная БД с применённой миграцией `head`."""
    if not _postgres_available():
        if os.environ.get("CI"):
            pytest.fail("В CI Postgres обязан быть доступен (сервис в ci.yml)")
        pytest.skip("Postgres недоступен — поднимите локальную среду: make dev")

    database_name = f"hospitality_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(f'CREATE DATABASE "{database_name}"'))
    try:
        settings = get_settings()
        alembic_config = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        alembic_config.set_main_option(
            "sqlalchemy.url",
            f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{database_name}",
        )
        command.upgrade(alembic_config, "head")
        yield database_name
    finally:
        asyncio.run(_admin_execute(f'DROP DATABASE "{database_name}" WITH (FORCE)'))


@pytest.fixture
async def canonical_database(
    migrated_database_name: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Направляет канонический путь (настройки → engine → session_scope) на временную БД."""
    monkeypatch.setenv("POSTGRES_DB", migrated_database_name)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        yield
    finally:
        await get_engine().dispose()
        get_settings.cache_clear()
        get_engine.cache_clear()


@pytest.fixture
def app_with_test_routes() -> FastAPI:
    app = create_app()

    @app.get("/echo-correlation-id")
    async def echo_correlation_id(request: Request) -> dict[str, str | None]:
        return {"correlation_id": get_correlation_id(request)}

    @app.get("/raise-app-error")
    async def raise_app_error() -> None:
        raise AppError(code="ERR-TEST-001", message="expected test error", status_code=418)

    @app.get("/raise-unhandled")
    async def raise_unhandled() -> None:
        raise RuntimeError("secret internals: database password is hunter2")

    @app.get("/validate/{item_id}")
    async def validate_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    return app


@pytest.fixture
def client(app_with_test_routes: FastAPI) -> TestClient:
    # raise_server_exceptions=False: необработанное исключение должно вернуться
    # клиенту как канонический 500-ответ, а не упасть внутрь теста.
    return TestClient(app_with_test_routes, raise_server_exceptions=False)
