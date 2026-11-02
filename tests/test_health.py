"""Task 0005: /health/live и /health/ready через httpx TestClient."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from hospitality.app import app
from hospitality.integrations.file_storage import LocalDiskFileStorageAdapter
from hospitality.shared import health
from hospitality.shared.config import get_settings
from hospitality.shared.db import get_engine

# Порт 1 требует прав root для bind — в тестовом окружении на нём гарантированно
# никто не слушает, поэтому соединение отклоняется мгновенно и детерминированно.
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
    async def reachable(*_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", reachable)
    monkeypatch.setattr(health, "check_redis", reachable)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "redis": "ok", "file_storage": "ok"},
    }


def test_readiness_fails_when_postgres_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(*_: str) -> bool:
        return False

    async def reachable(*_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", unreachable)
    monkeypatch.setattr(health, "check_redis", reachable)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "error", "redis": "ok", "file_storage": "ok"},
    }


def test_readiness_fails_when_redis_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(*_: str) -> bool:
        return False

    async def reachable(*_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", reachable)
    monkeypatch.setattr(health, "check_redis", unreachable)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "redis": "error", "file_storage": "ok"},
    }


def test_readiness_fails_when_file_storage_unwritable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spec 0047: том файлов, куда нельзя писать, — 503, а не «здоров» с теряющимися файлами."""

    async def reachable(*_: str) -> bool:
        return True

    monkeypatch.setattr(health, "check_postgres", reachable)
    monkeypatch.setattr(health, "check_redis", reachable)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("файл на месте каталога тома")
    monkeypatch.setattr(
        health, "file_storage_or_none", lambda: LocalDiskFileStorageAdapter(blocker)
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "redis": "ok", "file_storage": "error"},
    }


async def test_check_postgres_returns_false_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PORT", "1")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        assert await health.check_postgres() is False
    finally:
        await get_engine().dispose()
        get_settings.cache_clear()
        get_engine.cache_clear()


def test_check_redis_returns_false_when_unreachable() -> None:
    assert asyncio.run(health.check_redis(_UNREACHABLE_REDIS_URL)) is False


class _FakeSession:
    """Сессия пула: соединение выдано, поведение `SELECT 1` задаётся фейком."""

    def __init__(self, *, fails: bool = False, hangs: bool = False) -> None:
        self._fails = fails
        self._hangs = hangs

    async def execute(self, _statement: object) -> None:
        if self._hangs:
            await asyncio.sleep(10)
        if self._fails:
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))


def _session_scope_with(session: _FakeSession) -> object:
    @asynccontextmanager
    async def scope() -> AsyncIterator[_FakeSession]:
        yield session

    return scope


async def test_check_postgres_returns_false_when_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Регрессия: соединение выдано, но SELECT 1 падает. Readiness обязан увидеть
    # False (→ 503/"error"), а не необработанное исключение (→ 500).
    monkeypatch.setattr(
        health, "platform_session_scope", _session_scope_with(_FakeSession(fails=True))
    )

    assert await health.check_postgres() is False


async def test_check_postgres_gives_up_when_pool_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пул занят или база молчит — readiness отвечает «error» за секунды, а не висит."""
    monkeypatch.setattr(
        health, "platform_session_scope", _session_scope_with(_FakeSession(hangs=True))
    )
    monkeypatch.setattr(health, "_CONNECT_TIMEOUT_SECONDS", 0.05)

    assert await health.check_postgres() is False


async def test_readiness_checks_reuse_app_pool(canonical_database: None) -> None:
    """#487: `/health/ready` открыт снаружи — цикл проверок не открывает по
    соединению с базой на каждую, а берёт одно из пула приложения."""
    connects: list[object] = []

    def record_connect(*args: object) -> None:
        connects.append(args[0])

    engine = get_engine().sync_engine
    event.listen(engine, "connect", record_connect)
    try:
        results = [await health.check_postgres() for _ in range(5)]
    finally:
        event.remove(engine, "connect", record_connect)

    assert results == [True] * 5
    assert len(connects) == 1
