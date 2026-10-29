"""Чтение формы с файлами `platform/upload_form.py` (ревью PR #495, Н-2; spec 0049 Р-8).

Без БД: запрос Starlette собирается из тела, которое строит httpx, — тем же
multipart, что шлёт браузер. Главное: потолок тела срабатывает до разбора и до
чтения, а файл больше 1 МиБ не уходит во временный файл на диске.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from hospitality.platform.upload_form import (
    UploadBodyTooLargeError,
    UploadFormMalformedError,
    UploadTooManyFilesError,
    read_upload_form,
)

MB = 1024 * 1024
Receive = Callable[[], Awaitable[dict[str, Any]]]


def _multipart(
    fields: dict[str, str], files: list[tuple[str, bytes]]
) -> tuple[bytes, dict[str, str]]:
    built = httpx.Request(
        "POST",
        "https://test/upload",
        data=fields,
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )
    return built.read(), dict(built.headers)


def _request(
    body: bytes, headers: dict[str, str], *, chunk: int = 64 * 1024
) -> tuple[Request, list[int]]:
    """Запрос с телом кусками; второй элемент — сколько раз читали поток."""
    reads: list[int] = []
    chunks = [body[i : i + chunk] for i in range(0, len(body), chunk)] or [b""]

    async def receive() -> dict[str, Any]:
        index = len(reads)
        reads.append(index)
        if index >= len(chunks):
            return {"type": "http.disconnect"}
        return {
            "type": "http.request",
            "body": chunks[index],
            "more_body": index < len(chunks) - 1,
        }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope, receive), reads


async def test_fields_and_files_are_read_in_memory_without_temp_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_disk(*args: object, **kwargs: object) -> None:
        raise AssertionError("upload spooled to a temporary file on disk")

    monkeypatch.setattr(tempfile, "TemporaryFile", no_disk)
    big = b"%PDF-1.4\n" + bytes(3 * MB)
    body, headers = _multipart(
        {"bill_to": "ТОО «Ромашка»", "note": "БИН 940140000385"},
        [("rekv.pdf", big), ("photo.jpg", b"\xff\xd8\xff")],
    )
    request, _ = _request(body, headers)

    form = await read_upload_form(request, max_body_bytes=51 * MB, max_files=5)

    assert form.fields == {"bill_to": "ТОО «Ромашка»", "note": "БИН 940140000385"}
    assert [(part.filename, part.data) for part in form.files] == [
        ("rekv.pdf", big),
        ("photo.jpg", b"\xff\xd8\xff"),
    ]


async def test_declared_length_over_ceiling_is_refused_before_reading() -> None:
    body, headers = _multipart({"note": "x"}, [])
    headers["content-length"] = str(52 * MB)
    request, reads = _request(body, headers)

    with pytest.raises(UploadBodyTooLargeError):
        await read_upload_form(request, max_body_bytes=51 * MB, max_files=5)

    assert reads == []


async def test_body_without_length_is_cut_by_byte_count() -> None:
    body, headers = _multipart({}, [("big.pdf", bytes(3 * MB))])
    headers.pop("content-length", None)
    request, reads = _request(body, headers)

    with pytest.raises(UploadBodyTooLargeError):
        await read_upload_form(request, max_body_bytes=1 * MB, max_files=5)

    # Чтение остановилось на потолке, а не дочитало три мегабайта.
    assert len(reads) <= MB // (64 * 1024) + 1


async def test_lying_content_length_is_cut_by_byte_count() -> None:
    body, headers = _multipart({}, [("big.pdf", bytes(3 * MB))])
    headers["content-length"] = "100"
    request, _ = _request(body, headers)

    with pytest.raises(UploadBodyTooLargeError):
        await read_upload_form(request, max_body_bytes=1 * MB, max_files=5)


async def test_too_many_files() -> None:
    body, headers = _multipart({}, [(f"{i}.pdf", b"%PDF-") for i in range(6)])
    request, _ = _request(body, headers)

    with pytest.raises(UploadTooManyFilesError):
        await read_upload_form(request, max_body_bytes=51 * MB, max_files=5)


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param("application/json", id="json"),
        pytest.param("application/x-www-form-urlencoded", id="urlencoded"),
    ],
)
async def test_not_multipart_is_malformed(content_type: str) -> None:
    request, _ = _request(b"{}", {"content-type": content_type, "content-length": "2"})

    with pytest.raises(UploadFormMalformedError):
        await read_upload_form(request, max_body_bytes=51 * MB, max_files=5)


async def test_oversized_text_field_is_malformed() -> None:
    body, headers = _multipart({"note": "я" * 40_000}, [])
    request, _ = _request(body, headers)

    with pytest.raises(UploadFormMalformedError):
        await read_upload_form(request, max_body_bytes=51 * MB, max_files=5)
