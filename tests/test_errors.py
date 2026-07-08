"""Task 0007: канон ошибок — AppError с кодом каталога, единый конверт ответа,
ERR-PLATFORM-001/002/003 (FOUNDATION §10.5, R-8)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hospitality.shared.errors import (
    HTTP_ERROR_CODE,
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
    # Обработчик Exception живёт в ServerErrorMiddleware снаружи
    # CorrelationIdMiddleware — заголовок обязан ставить сам ответ (§10.2).
    assert response.headers[CORRELATION_ID_HEADER] == "err-2"


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


def test_framework_404_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/no-such-path", headers={CORRELATION_ID_HEADER: "err-404"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": HTTP_ERROR_CODE,
            "message": "Not Found",
            "correlation_id": "err-404",
        }
    }
    assert response.headers[CORRELATION_ID_HEADER] == "err-404"


def test_framework_405_keeps_allow_header(client: TestClient) -> None:
    response = client.post("/echo-correlation-id")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == HTTP_ERROR_CODE
    # Заголовки HTTPException доходят до клиента (Allow — требование RFC 9110).
    assert response.headers["allow"] == "GET"


def test_validation_error_returns_catalog_code(client: TestClient) -> None:
    response = client.get("/validate/not-a-number")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == VALIDATION_ERROR_CODE
    assert error["correlation_id"]  # присвоен автоматически
    assert isinstance(error["details"], list) and error["details"]


def test_validation_error_log_omits_raw_client_input(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()

    response = client.get("/validate/SECRET-GUEST-DATA-42")

    record = find_record(read_log_records(capsys), "request_validation_failed")
    # Сырой ввод клиента (поле input у Pydantic) в логи не пишется — PII (§10.1)...
    assert "SECRET-GUEST-DATA-42" not in json.dumps(record)
    assert record["details"] and set(record["details"][0]) == {"loc", "msg", "type"}
    # ...а в ответе клиенту его собственный ввод остаётся.
    assert "SECRET-GUEST-DATA-42" in response.text


def test_app_error_accepts_catalog_code_format() -> None:
    error = AppError(code="ERR-PLATFORM-001", message="boom", status_code=503)

    assert error.code == "ERR-PLATFORM-001"
    assert error.message == "boom"
    assert error.status_code == 503


@pytest.mark.parametrize("bad_code", ["OOPS-1", "err-platform-001", "ERR-PLATFORM-1", ""])
def test_app_error_rejects_malformed_code(bad_code: str) -> None:
    with pytest.raises(ValueError, match="ERR-<MODULE>-NNN"):
        AppError(code=bad_code, message="boom", status_code=400)


# Коды, встречающиеся в src/ только как учебные примеры в докстрингах, —
# статью в каталоге не требуют.
_DOC_EXAMPLE_CODES = {"ERR-REQUESTS-001"}


def test_every_error_code_in_src_has_catalog_article() -> None:
    """Правило каталога «код без статьи — блокер ревью» проверяет машина (дух R-9)."""
    code_pattern = re.compile(r"ERR-[A-Z0-9]+-\d{3}")
    repo_root = Path(__file__).resolve().parent.parent
    catalog = (repo_root / "docs" / "runbooks" / "errors.md").read_text(encoding="utf-8")
    documented = set(code_pattern.findall(catalog)) | _DOC_EXAMPLE_CODES

    for path in sorted((repo_root / "src").rglob("*.py")):
        for code in code_pattern.findall(path.read_text(encoding="utf-8")):
            assert code in documented, (
                f"{code} из {path.relative_to(repo_root)} не имеет статьи "
                "в docs/runbooks/errors.md (добавляется в том же PR)"
            )
