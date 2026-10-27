"""Сервис хранилища файлов на живом Postgres (spec 0047 §3–§5, §9; ADR-022).

Изоляция — по канону `tests/test_tenant_isolation.py`: чужой тенант не читает и не удаляет
файл по id, а UPDATE/DELETE без WHERE не дотягиваются до чужой строки.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult

from hospitality.integrations.file_storage import FakeFileStorageAdapter
from hospitality.platform import files
from hospitality.platform.file_crypto import (
    FileDecryptionError,
    decrypt_file,
    encrypt_file,
    validate_file_encryption_key,
)
from hospitality.platform.file_models import FilePurpose, UploadedFile
from hospitality.platform.files import (
    delete_expired_files,
    delete_file,
    file_response,
    read_file,
    store_upload,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.ports.file_storage import FileStorageError
from hospitality.shared.tenancy import tenant_context
from tests import file_samples

GUEST_DOCUMENT = FilePurpose.GUEST_DOCUMENT
MENU_PHOTO = FilePurpose.MENU_PHOTO


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def _row_count() -> int:
    async with session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(UploadedFile)) or 0)


async def _code_of(call: Any) -> tuple[str, int]:
    with pytest.raises(AppError) as error:
        await call
    return error.value.code, error.value.status_code


# --- Загрузка и чтение --------------------------------------------------------------------


async def test_image_roundtrip_stores_encrypted_webp_under_tenant_key(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        uploaded = await store_upload(GUEST_DOCUMENT, file_samples.png_with_exif(), "ТОО Ромашка.png")
        stored = await read_file(uploaded.id)

    [key] = fake_file_storage.stored_keys()
    assert key == f"{tenant_a}/guest_document/{uploaded.id}.webp"
    on_disk = await fake_file_storage.get(key)
    assert on_disk[:1] == b"\x01" and b"WEBP" not in on_disk  # шифротекст, а не картинка
    assert stored.content[:4] == b"RIFF" and stored.content[8:12] == b"WEBP"
    assert stored.file.content_type == "image/webp"
    assert stored.file.size_bytes == len(stored.content)
    assert stored.file.sha256 == hashlib.sha256(stored.content).hexdigest()
    assert stored.file.original_name == "ТОО Ромашка.webp"


async def test_pdf_and_docx_are_stored_byte_for_byte(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    document = file_samples.docx()
    with tenant_context(tenant_a):
        pdf = await store_upload(GUEST_DOCUMENT, file_samples.PDF, "счёт.exe")
        word = await store_upload(GUEST_DOCUMENT, document, "C:\\Users\\гость\\реквизиты.docx")

        assert (await read_file(pdf.id)).content == file_samples.PDF
        assert (await read_file(word.id)).content == document
    assert pdf.original_name == "счёт.exe.pdf"
    assert word.original_name == "реквизиты.docx"


@pytest.mark.parametrize(
    ("purpose", "data", "expected"),
    [
        (GUEST_DOCUMENT, b"%PDF-" + b"0" * (10 * 1024 * 1024), ("ERR-FILES-001", 413)),
        (MENU_PHOTO, file_samples.PDF, ("ERR-FILES-002", 415)),
        (GUEST_DOCUMENT, file_samples.SVG, ("ERR-FILES-002", 415)),
        (GUEST_DOCUMENT, b"plain text named photo.jpg", ("ERR-FILES-002", 415)),
        (GUEST_DOCUMENT, b"", ("ERR-FILES-002", 415)),
        (GUEST_DOCUMENT, file_samples.plain_zip(), ("ERR-FILES-002", 415)),
        (GUEST_DOCUMENT, file_samples.huge_png(), ("ERR-FILES-003", 422)),
        (
            GUEST_DOCUMENT,
            file_samples.docx(extra_names=("word/vbaProject.bin",)),
            ("ERR-FILES-004", 422),
        ),
    ],
    ids=["11mb", "pdf-as-menu", "svg", "text-as-jpg", "empty", "zip", "56mp", "macros"],
)
async def test_rejected_upload_leaves_nothing(
    fake_file_storage: FakeFileStorageAdapter,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    purpose: FilePurpose,
    data: bytes,
    expected: tuple[str, int],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        assert await _code_of(store_upload(purpose, data, "photo.jpg")) == expected
        assert await _row_count() == 0
    assert fake_file_storage.stored_keys() == []


async def test_upload_without_encryption_key_is_refused(
    fake_file_storage: FakeFileStorageAdapter,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    with tenant_context(two_tenants[0]):
        code = await _code_of(store_upload(GUEST_DOCUMENT, file_samples.PDF, "a.pdf"))
    assert code == ("ERR-FILES-006", 503)


async def test_upload_without_storage_is_refused(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    files.configure_file_storage(None)
    with tenant_context(two_tenants[0]):
        code = await _code_of(store_upload(GUEST_DOCUMENT, file_samples.PDF, "a.pdf"))
    assert code == ("ERR-FILES-006", 503)


class _FailingPutAdapter(FakeFileStorageAdapter):
    async def put(self, key: str, data: bytes) -> None:
        raise FileStorageError("disk full")


async def test_failed_write_leaves_no_row(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    files.configure_file_storage(_FailingPutAdapter())
    with tenant_context(two_tenants[0]):
        code = await _code_of(store_upload(GUEST_DOCUMENT, file_samples.PDF, "a.pdf"))
        assert await _row_count() == 0
    assert code == ("ERR-FILES-007", 503)


async def test_missing_or_swapped_blob_is_reported_not_served(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    with tenant_context(two_tenants[0]):
        first = await store_upload(GUEST_DOCUMENT, file_samples.PDF, "a.pdf")
        second = await store_upload(GUEST_DOCUMENT, file_samples.docx(), "b.docx")
    first_key, second_key = (
        f"{two_tenants[0]}/guest_document/{first.id}.pdf",
        f"{two_tenants[0]}/guest_document/{second.id}.docx",
    )
    # Шифротекст первого файла под ключом второго: дополнительные данные шифра не совпадут.
    await fake_file_storage.put(second_key, await fake_file_storage.get(first_key))
    await fake_file_storage.delete(first_key)

    with tenant_context(two_tenants[0]):
        assert await _code_of(read_file(first.id)) == ("ERR-FILES-007", 503)
        assert await _code_of(read_file(second.id)) == ("ERR-FILES-007", 503)


# --- Изоляция тенантов --------------------------------------------------------------------


async def test_other_tenant_cannot_read_or_delete_by_id(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        uploaded = await store_upload(GUEST_DOCUMENT, file_samples.PDF, "a.pdf")

    with tenant_context(tenant_b):
        assert await _code_of(read_file(uploaded.id)) == ("ERR-FILES-005", 404)
        assert await _code_of(delete_file(uploaded.id)) == ("ERR-FILES-005", 404)

    with tenant_context(tenant_a):
        assert (await read_file(uploaded.id)).content == file_samples.PDF


async def test_update_and_delete_without_where_do_not_reach_foreign_rows(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await store_upload(GUEST_DOCUMENT, file_samples.PDF, "a.pdf")

    with tenant_context(tenant_b):
        async with session_scope() as session:
            updated = await session.execute(text("UPDATE uploaded_files SET original_name = 'x'"))
            deleted = await session.execute(text("DELETE FROM uploaded_files"))
        assert cast("CursorResult[Any]", updated).rowcount == 0
        assert cast("CursorResult[Any]", deleted).rowcount == 0
        assert await _row_count() == 0

    with tenant_context(tenant_a):
        async with session_scope() as session:
            names = (await session.scalars(select(UploadedFile.original_name))).all()
    assert names == ["a.pdf"]


# --- Удаление и ретеншн -------------------------------------------------------------------


async def test_delete_removes_file_and_row(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    with tenant_context(two_tenants[0]):
        uploaded = await store_upload(MENU_PHOTO, file_samples.png_with_alpha(), "plov.png")
        await delete_file(uploaded.id)

        assert await _row_count() == 0
        assert await _code_of(read_file(uploaded.id)) == ("ERR-FILES-005", 404)
    assert fake_file_storage.stored_keys() == []


async def test_retention_deletes_old_guest_files_and_keeps_menu_photos(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    with tenant_context(two_tenants[0]):
        old_document = await store_upload(GUEST_DOCUMENT, file_samples.PDF, "old.pdf")
        fresh_document = await store_upload(GUEST_DOCUMENT, file_samples.PDF, "new.pdf")
        old_photo = await store_upload(MENU_PHOTO, file_samples.png_with_alpha(), "plov.png")
        async with session_scope() as session:
            await session.execute(
                update(UploadedFile)
                .where(UploadedFile.id.in_([old_document.id, old_photo.id]))
                .values(created_at=utc_now() - timedelta(days=91))
            )

        deleted = await delete_expired_files(created_before=utc_now() - timedelta(days=90))
        again = await delete_expired_files(created_before=utc_now() - timedelta(days=90))

        assert (deleted, again) == (1, 0)
        assert await _code_of(read_file(old_document.id)) == ("ERR-FILES-005", 404)
        assert (await read_file(fresh_document.id)).content == file_samples.PDF
        assert (await read_file(old_photo.id)).file.purpose is MENU_PHOTO
    assert len(fake_file_storage.stored_keys()) == 2


async def test_retention_with_nothing_to_delete_does_not_need_storage(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    files.configure_file_storage(None)
    with tenant_context(two_tenants[0]):
        assert await delete_expired_files(created_before=utc_now()) == 0


# --- Отдача, ключ, готовность -------------------------------------------------------------


async def test_response_headers_never_let_documents_open_in_browser(
    fake_file_storage: FakeFileStorageAdapter, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    with tenant_context(two_tenants[0]):
        photo = await read_file(
            (await store_upload(MENU_PHOTO, file_samples.png_with_alpha(), "plov.png")).id
        )
        document = await read_file(
            (await store_upload(GUEST_DOCUMENT, file_samples.PDF, "Счёт \"1\".pdf")).id
        )

    image_headers = file_response(photo).headers
    assert image_headers["content-disposition"] == "inline"
    assert image_headers["cache-control"] == "private, max-age=86400"
    pdf_headers = file_response(document).headers
    assert pdf_headers["content-type"] == "application/pdf"
    assert pdf_headers["content-disposition"].startswith("attachment; filename=")
    assert "filename*=UTF-8''%D0%A1%D1%87%D1%91%D1%82%20%221%22.pdf" in pdf_headers[
        "content-disposition"
    ]
    assert pdf_headers["cache-control"] == "no-store"
    for headers in (image_headers, pdf_headers):
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["content-security-policy"] == "default-src 'none'; sandbox"


async def test_storage_health_follows_configured_adapter(
    fake_file_storage: FakeFileStorageAdapter,
) -> None:
    assert await files.check_file_storage_health() is True
    files.configure_file_storage(None)
    assert await files.check_file_storage_health() is False


def test_ciphertext_is_bound_to_its_storage_key() -> None:
    key = b"k" * 32
    blob = encrypt_file(key, b"secret", b"tenant-a/guest_document/x.pdf")

    assert decrypt_file(key, blob, b"tenant-a/guest_document/x.pdf") == b"secret"
    with pytest.raises(FileDecryptionError):
        decrypt_file(key, blob, b"tenant-b/guest_document/x.pdf")
    with pytest.raises(FileDecryptionError):
        decrypt_file(key, b"\x02" + blob[1:], b"tenant-a/guest_document/x.pdf")


@pytest.mark.parametrize(
    "bad_key", ["c2VjcmV0LWtleS1ub3QtMzItYnl0ZXM=", "not base64 at all!"]
)
def test_bad_encryption_key_stops_start_without_printing_it(
    monkeypatch: pytest.MonkeyPatch, bad_key: str
) -> None:
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", bad_key)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as stop:
            validate_file_encryption_key()
    finally:
        get_settings.cache_clear()
    assert bad_key not in str(stop.value)
    assert "FILE_ENCRYPTION_KEY" in str(stop.value)
