"""#346: отказ БД не выносит текст гостя в лог, Sentry и `outbox_events.last_error`.

SQLAlchemy по умолчанию дописывает к тексту исключения значения параметров
запроса (`[parameters: (...)]`), а среди них — текст гостя. Движок приложения
создаётся с `hide_parameters=True` (`shared/db.py`); значения строки в ответе
Postgres (DETAIL) скрывает RLS тенантной таблицы. Маркер — латиницей: JSON-лог
экранирует кириллицу, и кириллический маркер прошёл бы проверку «нет в логе»
ложно.
"""

from __future__ import annotations

import json
import uuid
from typing import ClassVar

import pytest
import sentry_sdk
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from hospitality.app import create_app
from hospitality.platform.models import Tenant, TenantIsolationCanary
from hospitality.shared.config import Settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.events import (
    DomainEvent,
    OutboxEvent,
    deliver_pending_events,
    publish,
    subscribe,
)
from hospitality.shared.logging import configure_logging
from hospitality.shared.sentry import init_sentry
from hospitality.shared.tenancy import tenant_context
from tests.test_sentry import CapturingTransport

MARKER = "GUESTLEAK305"
GUEST_TEXT = f"во сколько завтрак, я Иванов из 305 {MARKER}"
HIDDEN = "[SQL parameters hidden due to hide_parameters=True]"


class GuestTextSaved(DomainEvent):
    """Событие, подписчик которого падает на записи текста гостя."""

    event_name: ClassVar[str] = "test.guest_text_saved"


@pytest.fixture
async def tenant_id(canonical_database: None) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-a", name="Hotel A")
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _insert_guest_text_twice() -> None:
    """Вторая вставка с тем же id падает IntegrityError (как конфликт ключа
    идемпотентности) — с текстом гостя среди параметров запроса."""
    row_id = uuid.uuid4()
    async with session_scope() as session:
        session.add(TenantIsolationCanary(id=row_id, note=GUEST_TEXT))
    async with session_scope() as session:
        session.add(TenantIsolationCanary(id=row_id, note=GUEST_TEXT))


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


async def test_db_error_text_hides_guest_text_but_keeps_diagnosis(tenant_id: uuid.UUID) -> None:
    with tenant_context(tenant_id), pytest.raises(DBAPIError) as caught:
        await _insert_guest_text_twice()

    message = str(caught.value)
    assert HIDDEN in message
    assert [str(exc) for exc in _exception_chain(caught.value) if MARKER in str(exc)] == []
    # Диагноз на месте: какой запрос и что нарушено.
    assert "INSERT INTO tenant_isolation_canary" in message
    assert "UniqueViolationError" in message


async def test_unhandled_db_error_keeps_guest_text_out_of_log_and_sentry(
    tenant_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = CapturingTransport()
    # init_sentry — до create_app: интеграции Starlette/FastAPI инструментируют приложение.
    init_sentry(
        Settings(sentry_dsn="https://public@sentry.invalid/1", sentry_environment="test"),
        transport=transport,
    )
    try:
        app = create_app()

        @app.get("/db-boom")
        async def db_boom() -> None:
            with tenant_context(tenant_id):
                await _insert_guest_text_twice()

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://test",
        ) as client:
            response = await client.get("/db-boom")
        sentry_sdk.get_client().flush()
    finally:
        # Глобальный клиент не должен протекать в другие тесты процесса.
        sentry_sdk.get_global_scope().set_client(None)

    assert response.status_code == 500
    log_output = capsys.readouterr().out
    assert '"unhandled_error"' in log_output
    assert HIDDEN in log_output  # трейсбек ошибки БД в логе есть…
    assert MARKER not in log_output  # …а текста гостя в нём нет
    sentry_payload = json.dumps(transport.events, default=str)
    assert HIDDEN in sentry_payload
    assert MARKER not in sentry_payload


async def test_failed_delivery_keeps_guest_text_out_of_last_error(
    tenant_id: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    async def save_guest_text(event: GuestTextSaved) -> None:
        await _insert_guest_text_twice()

    configure_logging()  # JSON в stdout, как в процессе воркера
    subscribe(GuestTextSaved, save_guest_text)
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await publish(session, GuestTextSaved())

    assert await deliver_pending_events(backoff_base_seconds=0) == 1
    async with platform_session_scope() as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()

    # `last_error` уходит и в алерт ERR-EVENTS-002 (`shared/outbox_alerts.py`).
    assert row.last_error is not None
    assert HIDDEN in row.last_error
    assert MARKER not in row.last_error
    log_output = capsys.readouterr().out
    assert '"event_delivery_failed"' in log_output
    assert MARKER not in log_output
