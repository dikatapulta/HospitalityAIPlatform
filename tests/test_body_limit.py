"""Предел тела запроса и потолок ответа 422 (issue #968, находка Л-3 аудита 10.11).

Аноним слал JSON из тысяч лишних ключей: имя каждого ложилось в строку лога, ввод —
в ответ на каждое нарушение, а тело в десятки мегабайт разбиралось целиком.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from hospitality.platform.upload_form import UploadBodyTooLargeError, read_upload_form
from hospitality.shared.body_limit import (
    BODY_TOO_LARGE_ERROR_CODE,
    DEFAULT_MAX_BODY_BYTES,
    raise_body_limit,
)
from hospitality.shared.errors import (
    MAX_LOC_PART_CHARS,
    MAX_VALIDATION_DETAILS,
    VALIDATION_ERROR_CODE,
    AppError,
)
from hospitality.shared.logging import configure_logging
from hospitality.shared.middleware import CORRELATION_ID_HEADER
from tests.test_logging import find_record, read_log_records

_RAISED_LIMIT = 2 * DEFAULT_MAX_BODY_BYTES


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


@pytest.fixture
def body_client(app_with_test_routes: FastAPI) -> TestClient:
    app = app_with_test_routes
    seen: list[str] = []
    app.state.seen = seen

    @app.post("/strict")
    async def strict(body: _StrictBody) -> dict[str, str]:
        seen.append(body.name)
        return {"name": body.name}

    @app.post("/raised")
    async def raised(request: Request) -> dict[str, int]:
        raise_body_limit(request, _RAISED_LIMIT)
        return {"size": len(await request.body())}

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        try:
            form = await read_upload_form(request, max_body_bytes=_RAISED_LIMIT, max_files=1)
        except UploadBodyTooLargeError as exc:
            raise AppError(code="ERR-TEST-001", message="form too large", status_code=413) from exc
        return {"size": len(form.files[0].data)}

    return TestClient(app, raise_server_exceptions=False)


def _extra_keys(count: int) -> bytes:
    return json.dumps({f"k{i}": 0 for i in range(count)}).encode()


def test_ten_thousand_extra_keys_bound_log_and_response(
    body_client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    body = _extra_keys(10_000)  # имени нет — ещё и «missing» с input на всё тело

    response = body_client.post(
        "/strict", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == VALIDATION_ERROR_CODE
    assert len(error["details"]) == MAX_VALIDATION_DETAILS
    assert all(set(item) == {"loc", "msg", "type"} for item in error["details"])
    assert len(response.content) < 5_000  # было: ввод клиента на каждое нарушение
    record = find_record(read_log_records(capsys), "request_validation_failed")
    assert record["error_count"] == 10_001
    assert len(record["details"]) == MAX_VALIDATION_DETAILS
    assert len(json.dumps(record)) < 5_000


def test_long_key_name_is_clipped_in_log_and_response(
    body_client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    key = "x" * 100_000

    response = body_client.post("/strict", json={"name": "a", key: 1})

    assert response.status_code == 422
    loc = response.json()["error"]["details"][0]["loc"]
    assert loc == ["body", "x" * MAX_LOC_PART_CHARS]
    record = find_record(read_log_records(capsys), "request_validation_failed")
    assert record["details"][0]["loc"] == loc


def test_body_over_limit_is_413_before_parsing(
    body_client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    body = json.dumps({"name": "a" * DEFAULT_MAX_BODY_BYTES}).encode()

    response = body_client.post(
        "/strict",
        content=body,
        headers={"content-type": "application/json", CORRELATION_ID_HEADER: "big-1"},
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": BODY_TOO_LARGE_ERROR_CODE,
            "message": "Request body is too large",
            "correlation_id": "big-1",
        }
    }
    assert body_client.app.state.seen == []  # type: ignore[attr-defined]
    records = read_log_records(capsys)
    rejected = find_record(records, "request_body_too_large")
    assert rejected["limit_bytes"] == DEFAULT_MAX_BODY_BYTES
    assert rejected["content_length"] == len(body)
    assert not [r for r in records if r.get("event") == "request_validation_failed"]
    assert find_record(records, "http_request")["status_code"] == 413


def test_body_without_length_is_cut_by_counted_bytes(body_client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"name": "'
        for _ in range(17):
            yield b"a" * 65_536
        yield b'"}'

    response = body_client.post(
        "/strict", content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == BODY_TOO_LARGE_ERROR_CODE
    assert body_client.app.state.seen == []  # type: ignore[attr-defined]


def test_body_within_limit_passes(body_client: TestClient) -> None:
    name = "a" * (DEFAULT_MAX_BODY_BYTES - 100)

    response = body_client.post("/strict", json={"name": name})

    assert response.status_code == 200
    assert body_client.app.state.seen == [name]  # type: ignore[attr-defined]


def test_route_raises_its_own_limit_before_reading(body_client: TestClient) -> None:
    allowed = body_client.post("/raised", content=b"a" * (_RAISED_LIMIT - 1))
    rejected = body_client.post("/raised", content=b"a" * (_RAISED_LIMIT + 1))

    assert (allowed.status_code, allowed.json()) == (200, {"size": _RAISED_LIMIT - 1})
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == BODY_TOO_LARGE_ERROR_CODE


def test_showroom_door_answers_extra_keys_without_amplification(client: TestClient) -> None:
    """Дверь из находки: витрина смонтирована всегда, тело проверяется раньше отеля и лимитов."""
    response = client.post(
        "/showroom/no-such-hotel/access",
        content=_extra_keys(10_000),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert len(response.json()["error"]["details"]) == MAX_VALIDATION_DETAILS
    assert len(response.content) < 5_000


def test_upload_form_raises_limit_and_keeps_its_own_refusal(body_client: TestClient) -> None:
    """Файлы больше общего предела проходят; сверх потолка формы — код маршрута, а не 011."""
    file = b"a" * (DEFAULT_MAX_BODY_BYTES + 1)
    allowed = body_client.post("/upload", files={"file": ("a.bin", file)})

    def chunks() -> Iterator[bytes]:
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n\r\n'
        for _ in range(3 * 16):
            yield b"a" * 65_536
        yield b"\r\n--b--\r\n"

    rejected = body_client.post(
        "/upload", content=chunks(), headers={"content-type": "multipart/form-data; boundary=b"}
    )

    assert (allowed.status_code, allowed.json()) == (200, {"size": len(file)})
    assert (rejected.status_code, rejected.json()["error"]["code"]) == (413, "ERR-TEST-001")
