"""Общие фикстуры тестов (Task 0007).

Приложение собирается настоящим composition root (`create_app`) — так тесты
проверяют и сам канон, и его подключение. Служебные роуты добавляются поверх,
чтобы не тащить тестовый код в приложение.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hospitality.app import create_app
from hospitality.shared.errors import AppError
from hospitality.shared.middleware import get_correlation_id


@pytest.fixture(autouse=True)
def _clean_log_context() -> Iterator[None]:
    # Контекст логирования не должен утекать между тестами.
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


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
