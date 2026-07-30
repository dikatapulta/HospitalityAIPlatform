"""Task 0007: канон логирования — обязательные поля §10.1, correlation id,
событие http_request."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from hospitality.shared.logging import configure_logging, get_logger, redact_secrets_in_path
from hospitality.shared.middleware import CORRELATION_ID_HEADER, CorrelationIdMiddleware

REQUIRED_FIELDS = (
    "timestamp",
    "level",
    "tenant_id",
    "correlation_id",
    "trace_id",
    "module",
    "event",
)


def read_log_records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    output = capsys.readouterr().out
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def find_record(records: list[dict[str, Any]], event: str) -> dict[str, Any]:
    matches = [record for record in records if record.get("event") == event]
    assert len(matches) == 1, f"expected exactly one {event!r} record, got {len(matches)}"
    return matches[0]


def test_log_record_contains_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    get_logger(module="tests.canon").info("canon_event", extra_field="value")

    record = find_record(read_log_records(capsys), "canon_event")
    for field in REQUIRED_FIELDS:
        assert field in record, f"required field {field!r} missing (FOUNDATION §10.1)"
    assert record["module"] == "tests.canon"
    assert record["level"] == "info"
    assert record["extra_field"] == "value"
    # Вне HTTP-контекста поля контекста присутствуют, но пусты.
    assert record["tenant_id"] is None
    assert record["correlation_id"] is None


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    configure_logging()

    assert len(logging.getLogger().handlers) == 1

    get_logger(module="tests.canon").info("logged_once")

    records = [r for r in read_log_records(capsys) if r.get("event") == "logged_once"]
    assert len(records) == 1


def test_httpx_logger_is_muted_below_warning(capsys: pytest.CaptureFixture[str]) -> None:
    # httpx на INFO пишет полный URL исходящего запроса; у Telegram Bot API токен
    # бота — часть пути, поэтому INFO-запись утекла бы секретом в логи (§11, #63).
    # configure_logging обязан заглушить httpx до WARNING, независимо от уровня
    # приложения.
    configure_logging("INFO")

    httpx_logger = logging.getLogger("httpx")
    assert httpx_logger.getEffectiveLevel() >= logging.WARNING

    httpx_logger.info("POST https://api.telegram.org/bot<TOKEN>/sendMessage")

    leaked = [r for r in read_log_records(capsys) if "api.telegram.org" in json.dumps(r)]
    assert leaked == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # Экран согласия bind-ссылки и потребление токена (spec 0033 §6).
        ("/w/hotel-astana/b/x7Kq9vLm2pR4tZ", "/w/hotel-astana/b/***"),
        ("/w/hotel-astana/b/x7Kq9vLm2pR4tZ/session", "/w/hotel-astana/b/***/session"),
        # Полный URL — так путь приходит из события Sentry.
        ("https://necturn.com/w/h/b/SECRET", "https://necturn.com/w/h/b/***"),
        # Маршрут остаётся читаемым: маскируется секрет, не весь путь.
        ("/g/hotel-astana/101/messages", "/g/hotel-astana/101/messages"),
        ("/staff/hotel-astana/checkin", "/staff/hotel-astana/checkin"),
    ],
)
def test_redact_secrets_in_path(path: str, expected: str) -> None:
    assert redact_secrets_in_path(path) == expected


def test_http_request_log_masks_bind_link_token(capsys: pytest.CaptureFixture[str]) -> None:
    """Токен bind-ссылки живёт в пути URL и без маскирования уходил бы в
    access-log открытым текстом — а оттуда в Sentry breadcrumbs (§11, ревью
    PR #155). Тот же класс утечки, что закрывает глушение httpx выше."""
    configure_logging()
    token = "x7Kq9vLm2pR4tZ-live-credential"

    async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def call() -> None:
        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": f"/w/hotel-astana/b/{token}",
            "headers": [],
            "state": {},
        }
        await CorrelationIdMiddleware(ok_app)(scope, _no_messages, _drop_message)

    asyncio.run(call())

    records = read_log_records(capsys)
    assert token not in json.dumps(records), "токен утёк в access-log"
    assert find_record(records, "http_request")["path"] == "/w/hotel-astana/b/***"


async def _no_messages() -> Message:  # pragma: no cover — receive не зовётся
    return {"type": "http.request"}


async def _drop_message(message: Message) -> None:
    return None


def test_stdlib_logs_render_as_same_json(capsys: pytest.CaptureFixture[str]) -> None:
    # Логи сторонних библиотек (uvicorn и т.п.) идут через stdlib logging
    # и обязаны выходить тем же JSON с теми же обязательными полями.
    configure_logging()

    logging.getLogger("uvicorn.error").warning("server boom")

    record = find_record(read_log_records(capsys), "server boom")
    for field in REQUIRED_FIELDS:
        assert field in record
    assert record["module"] == "uvicorn.error"
    assert record["level"] == "warning"


def test_correlation_id_from_header_is_propagated(client: TestClient) -> None:
    response = client.get("/echo-correlation-id", headers={CORRELATION_ID_HEADER: "req-canon-123"})

    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER] == "req-canon-123"
    assert response.json() == {"correlation_id": "req-canon-123"}


def test_correlation_id_generated_when_header_missing(client: TestClient) -> None:
    response = client.get("/echo-correlation-id")

    generated = response.headers[CORRELATION_ID_HEADER]
    uuid.UUID(generated)  # генерируем валидный UUID
    assert response.json() == {"correlation_id": generated}


def test_unsafe_correlation_id_is_replaced(client: TestClient) -> None:
    # Слишком длинные / небезопасные значения не попадают в логи как есть.
    unsafe_value = "x" * 200

    response = client.get("/echo-correlation-id", headers={CORRELATION_ID_HEADER: unsafe_value})

    replaced = response.headers[CORRELATION_ID_HEADER]
    assert replaced != unsafe_value
    uuid.UUID(replaced)


def test_http_request_log_written_for_each_request(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()

    client.get("/echo-correlation-id", headers={CORRELATION_ID_HEADER: "req-log-1"})

    record = find_record(read_log_records(capsys), "http_request")
    assert record["method"] == "GET"
    assert record["path"] == "/echo-correlation-id"
    assert record["status_code"] == 200
    assert record["correlation_id"] == "req-log-1"
    assert record["module"] == "hospitality.shared.middleware"
    assert "duration_ms" in record
