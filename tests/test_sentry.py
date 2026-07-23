"""Task 0018: Sentry получает необработанные ошибки с контекстом (§10.4).

Транспорт подменяется in-memory перехватом — реальный DSN и сеть не нужны.
Порядок фикстур в сигнатуре важен: init_sentry должен отработать ДО
create_app, чтобы интеграции Starlette/FastAPI инструментировали приложение.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event
from starlette.datastructures import Headers
from starlette.types import Scope

from hospitality.app import create_app
from hospitality.shared.config import Settings
from hospitality.shared.errors import AppError
from hospitality.shared.sentry import init_sentry

TEST_TENANT_ID = uuid.uuid4()


class CapturingTransport(Transport):
    """In-memory транспорт: события складываются в список вместо сети."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def captured_sentry_events() -> Iterator[list[Event]]:
    transport = CapturingTransport()
    init_sentry(
        Settings(sentry_dsn="https://public@sentry.invalid/1", sentry_environment="test"),
        transport=transport,
    )
    yield transport.events
    sentry_sdk.get_client().flush()
    # Глобальный клиент не должен протекать в другие тесты процесса.
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def sentry_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Приложение настоящим composition root, но с фейковым резолвером тенанта:
    контекст тенанта устанавливает настоящий TenantContextMiddleware — тест
    проверяет тот же путь биндинга, что и на staging (без БД)."""

    async def fake_resolver(scope: Scope) -> uuid.UUID | None:
        if Headers(scope=scope).get("Authorization") == "Bearer sentry-test-token":
            return TEST_TENANT_ID
        return None

    monkeypatch.setattr("hospitality.app.resolve_tenant_from_service_token", fake_resolver)
    app = create_app()

    @app.get("/sentry-boom")
    async def sentry_boom() -> None:
        raise RuntimeError("sentry test explosion")

    @app.get("/sentry-expected")
    async def sentry_expected() -> None:
        raise AppError(code="ERR-TEST-001", message="expected error", status_code=418)

    return app


def test_unhandled_error_reaches_sentry_with_tenant_and_correlation_id(
    captured_sentry_events: list[Event], sentry_app: FastAPI
) -> None:
    client = TestClient(sentry_app, raise_server_exceptions=False)

    response = client.get("/sentry-boom", headers={"Authorization": "Bearer sentry-test-token"})
    sentry_sdk.get_client().flush()

    assert response.status_code == 500
    assert captured_sentry_events, "необработанная ошибка обязана породить событие"
    for event in captured_sentry_events:
        tags = event.get("tags", {})
        assert tags.get("tenant_id") == str(TEST_TENANT_ID)
        assert tags.get("correlation_id") == response.headers["X-Correlation-ID"]
        assert event.get("environment") == "test"


def test_expected_app_error_does_not_create_sentry_event(
    captured_sentry_events: list[Event], sentry_app: FastAPI
) -> None:
    """AppError — ожидаемая ошибка (§10.5): её диагностирует каталог, не трекер."""
    client = TestClient(sentry_app)

    response = client.get("/sentry-expected")
    sentry_sdk.get_client().flush()

    assert response.status_code == 418
    assert captured_sentry_events == []


def test_empty_dsn_leaves_sentry_disabled() -> None:
    init_sentry(Settings(sentry_dsn=""))

    assert not sentry_sdk.get_client().is_active()
