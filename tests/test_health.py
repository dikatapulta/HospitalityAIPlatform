"""Task 0005: /health/live и /health/ready через httpx TestClient."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from hospitality.app import app
from hospitality.shared import health

# Порт 1 требует прав root для bind — в тестовом окружении на нём гарантированно
# никто не слушает, поэтому соединение отклоняется мгновенно и детерминированно.
_UNREACHABLE_POSTGRES_DSN = "postgresql://user:pass@127.0.0.1:1/db"
_UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_liveness_returns_ok(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_ok_when_dependencies_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reachable(_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", reachable)
    monkeypatch.setattr(health, "check_redis", reachable)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


def test_readiness_fails_when_postgres_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(_: str) -> bool:
        return False

    async def reachable(_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", unreachable)
    monkeypatch.setattr(health, "check_redis", reachable)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "error", "redis": "ok"},
    }


def test_readiness_fails_when_redis_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(_: str) -> bool:
        return False

    async def reachable(_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", reachable)
    monkeypatch.setattr(health, "check_redis", unreachable)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "redis": "error"},
    }


def test_check_postgres_returns_false_when_unreachable() -> None:
    assert asyncio.run(health.check_postgres(_UNREACHABLE_POSTGRES_DSN)) is False


def test_check_redis_returns_false_when_unreachable() -> None:
    assert asyncio.run(health.check_redis(_UNREACHABLE_REDIS_URL)) is False
