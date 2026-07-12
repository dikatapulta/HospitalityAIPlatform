"""Юнит-тесты контекста тенанта (Task 0009): contextvar, логи, middleware.

Изоляция на уровне БД (RLS, SET LOCAL) — в обязательном
`tests/test_tenant_isolation.py`; здесь — поведение самого контекста.
"""

from __future__ import annotations

import uuid

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.types import Scope

from hospitality.shared.tenancy import (
    TenantContextMiddleware,
    TenantContextRequiredError,
    current_tenant_id,
    current_tenant_id_or_none,
    tenant_context,
)

TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def test_current_tenant_id_raises_outside_context() -> None:
    with pytest.raises(TenantContextRequiredError, match="tenant context is not set"):
        current_tenant_id()


def test_tenant_context_sets_and_restores() -> None:
    assert current_tenant_id_or_none() is None
    with tenant_context(TENANT_A):
        assert current_tenant_id() == TENANT_A
    assert current_tenant_id_or_none() is None


def test_tenant_context_restores_on_exception() -> None:
    with pytest.raises(RuntimeError, match="expected"), tenant_context(TENANT_A):
        raise RuntimeError("expected")
    assert current_tenant_id_or_none() is None


def test_nested_tenant_context_restores_outer() -> None:
    with tenant_context(TENANT_A):
        with tenant_context(TENANT_B):
            assert current_tenant_id() == TENANT_B
        assert current_tenant_id() == TENANT_A


def test_tenant_context_binds_and_restores_log_context() -> None:
    with tenant_context(TENANT_A):
        assert structlog.contextvars.get_contextvars()["tenant_id"] == str(TENANT_A)
    assert "tenant_id" not in structlog.contextvars.get_contextvars()


# ---------------------------------------------------------------------------
# TenantContextMiddleware
# ---------------------------------------------------------------------------


async def _tenant_from_test_header(scope: Scope) -> uuid.UUID | None:
    # Тестовый резолвер. В приложении тенант из клиентских заголовков не берётся
    # никогда (§11) — реальный резолвер ищет тенанта по сервисному токену
    # (`resolve_tenant_from_service_token`, platform/auth.py, Task 0013).
    value = Headers(scope=scope).get("X-Test-Tenant-ID")
    return uuid.UUID(value) if value else None


@pytest.fixture
def middleware_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(TenantContextMiddleware, resolver=_tenant_from_test_header)

    @app.get("/whoami")
    async def whoami() -> dict[str, str | None]:
        tenant_id = current_tenant_id_or_none()
        return {"tenant_id": str(tenant_id) if tenant_id else None}

    return TestClient(app)


def test_middleware_binds_resolved_tenant(middleware_client: TestClient) -> None:
    response = middleware_client.get("/whoami", headers={"X-Test-Tenant-ID": str(TENANT_A)})
    assert response.json() == {"tenant_id": str(TENANT_A)}


def test_middleware_without_resolution_leaves_context_empty(
    middleware_client: TestClient,
) -> None:
    response = middleware_client.get("/whoami")
    assert response.json() == {"tenant_id": None}


def test_middleware_does_not_leak_between_sequential_requests(
    middleware_client: TestClient,
) -> None:
    first = middleware_client.get("/whoami", headers={"X-Test-Tenant-ID": str(TENANT_A)})
    second = middleware_client.get("/whoami")
    third = middleware_client.get("/whoami", headers={"X-Test-Tenant-ID": str(TENANT_B)})
    assert first.json() == {"tenant_id": str(TENANT_A)}
    assert second.json() == {"tenant_id": None}
    assert third.json() == {"tenant_id": str(TENANT_B)}


def test_request_without_token_passes_through_without_tenant(client: TestClient) -> None:
    # Composition root подключает middleware с резолвером по сервисному токену
    # (Task 0013): запрос без заголовка Authorization идёт дальше без контекста
    # тенанта (и без обращения к БД) — неаутентифицированные роуты (health,
    # OpenAPI) работают как раньше.
    response = client.get("/health/live")
    assert response.status_code == 200
    assert current_tenant_id_or_none() is None
