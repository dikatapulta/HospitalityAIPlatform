"""Task 0007: канон ошибок — AppError с кодом каталога, единый конверт ответа,
ERR-PLATFORM-001/002 (FOUNDATION §10.5, R-8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hospitality.shared.errors import (
    INTERNAL_ERROR_CODE,
    VALIDATION_ERROR_CODE,
    AppError,
)
from hospitality.shared.logging import configure_logging
from hospitality.shared.middleware import CORRELATION_ID_HEADER
from tests.test_logging import find_record, read_log_records


def test_app_error_is_serialized_with_code(client: TestClient) -> None:
    response = client.get("/raise-app-error", headers={CORRELATION_ID_HEADER: "err-1"})

    assert response.status_code == 418
    assert response.json() == {
        "error": {
            "code": "ERR-TEST-001",
            "message": "expected test error",
            "correlation_id": "err-1",
        }
    }
    assert response.headers[CORRELATION_ID_HEADER] == "err-1"


def test_unhandled_error_returns_internal_code_without_details(client: TestClient) -> None:
    response = client.get("/raise-unhandled", headers={CORRELATION_ID_HEADER: "err-2"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == INTERNAL_ERROR_CODE
    assert body["error"]["message"] == "Internal server error"
    assert body["error"]["correlation_id"] == "err-2"
    # Внутренности исключения не утекают клиенту (FOUNDATION §11).
    assert "secret internals" not in response.text
    assert "hunter2" not in response.text


def test_unhandled_error_is_logged_with_traceback(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()

    client.get("/raise-unhandled", headers={CORRELATION_ID_HEADER: "err-3"})

    records = read_log_records(capsys)
    error_record = find_record(records, "unhandled_error")
    assert error_record["correlation_id"] == "err-3"
    assert error_record["error_type"] == "RuntimeError"
    assert "RuntimeError" in error_record["exception"]  # трасса стека — в логах

    request_record = find_record(records, "http_request")
    assert request_record["status_code"] == 500


def test_validation_error_returns_catalog_code(client: TestClient) -> None:
    response = client.get("/validate/not-a-number")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == VALIDATION_ERROR_CODE
    assert error["correlation_id"]  # присвоен автоматически
    assert isinstance(error["details"], list) and error["details"]


def test_app_error_accepts_catalog_code_format() -> None:
    error = AppError(code="ERR-PLATFORM-001", message="boom", status_code=503)

    assert error.code == "ERR-PLATFORM-001"
    assert error.message == "boom"
    assert error.status_code == 503


@pytest.mark.parametrize("bad_code", ["OOPS-1", "err-platform-001", "ERR-PLATFORM-1", ""])
def test_app_error_rejects_malformed_code(bad_code: str) -> None:
    with pytest.raises(ValueError, match="ERR-<MODULE>-NNN"):
        AppError(code=bad_code, message="boom", status_code=400)
