"""Сервис файлов тенанта `platform/files.py` (spec 0047 §3–§7, §9).

Живой Postgres (временная БД с миграциями) и Fake-адаптер хранилища в порту.
Изоляция тенантов — по образцу `tests/test_tenant_isolation.py`: чужой id не
читается и не удаляется, UPDATE/DELETE без WHERE не дотягиваются до чужих
строк, строку с чужим tenant_id не вставить. Ретеншн — в тестах `channels/common`.
"""

from __future__ import annotations

import sys
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from hospitality.integrations.file_storage import FakeFileStorageAdapter
from hospitality.platform import files
from hospitality.platform.files import FilePurpose, UploadedFile
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.test_file_content import make_docx, make_png

PDF = b"%PDF-1.4\nBIN 123456789012 IIK KZ000000000000000000\n"

Tenants = tuple[uuid.UUID, uuid.UUID]


@pytest.fixture
async def two_tenants(canonical_database: None) -> Tenants:
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def _rows(tenant_id: uuid.UUID) -> list[UploadedFile]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return list(await session.scalars(select(UploadedFile)))


async def test_store_and_read_round_trip_is_encrypted_at_rest(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        stored = await files.store_upload(
            FilePurpose.GUEST_DOCUMENT, PDF, "C:\\Users\\guest\\..\\Реквизиты ТОО «Ромашка».PDF"
        )
        read = await files.read_file(stored.id)

    assert read.data == PDF
    assert read.meta == stored
    assert stored.original_name == "Реквизиты ТОО «Ромашка».pdf"
    assert (stored.content_type, stored.size_bytes) == ("application/pdf", len(PDF))
    [key] = fake_file_storage.keys()
    assert key.startswith(f"{tenant_a}/guest_document/") and key.endswith(".pdf")
    blob = await fake_file_storage.get(key)
    assert b"123456789012" not in blob


async def test_foreign_tenant_cannot_read_or_delete_by_id(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        stored = await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, "a.pdf")

    with tenant_context(tenant_b):
        for operation in (files.read_file, files.delete_file):
            with pytest.raises(AppError) as caught:
                await operation(stored.id)
            assert (caught.value.code, caught.value.status_code) == ("ERR-FILES-004", 404)

    assert len(await _rows(tenant_a)) == 1
    assert len(fake_file_storage.keys()) == 1


async def test_rls_isolates_rows_for_raw_sql_update_and_delete(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, "a.pdf")
    with tenant_context(tenant_b):
        await files.store_upload(FilePurpose.MENU_PHOTO, make_png(), "b.png")
        async with session_scope() as session:
            raw = (await session.execute(text("SELECT original_name FROM uploaded_files"))).all()
            assert [row[0] for row in raw] == ["b.webp"]
            await session.execute(text("UPDATE uploaded_files SET original_name = 'x.pdf'"))
            await session.execute(text("DELETE FROM uploaded_files"))

    assert [row.original_name for row in await _rows(tenant_a)] == ["a.pdf"]
    assert await _rows(tenant_b) == []


async def test_insert_with_foreign_tenant_id_is_rejected(two_tenants: Tenants) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(
                UploadedFile(
                    tenant_id=tenant_b,
                    purpose="guest_document",
                    storage_key=f"{tenant_b}/guest_document/{uuid.uuid4().hex}.pdf",
                    content_type="application/pdf",
                    size_bytes=1,
                    sha256="0" * 64,
                    original_name="x.pdf",
                )
            )
            await session.flush()


@pytest.mark.parametrize(
    ("purpose", "payload", "code", "status", "message_part"),
    [
        (
            FilePurpose.GUEST_DOCUMENT,
            b"%PDF" + bytes(11 * 1024 * 1024),
            "ERR-FILES-001",
            413,
            "5 МБ",
        ),
        (FilePurpose.MENU_PHOTO, b"not an image", "ERR-FILES-002", 415, "JPG, PNG или WebP"),
        (FilePurpose.GUEST_DOCUMENT, b"<svg/>", "ERR-FILES-002", 415, "PDF или документ Word"),
        (
            FilePurpose.GUEST_DOCUMENT,
            make_docx(extra_entries={"word/vbaProject.bin": b"x"}),
            "ERR-FILES-003",
            415,
            "макросы",
        ),
    ],
)
async def test_rejected_upload_stores_nothing(
    two_tenants: Tenants,
    fake_file_storage: FakeFileStorageAdapter,
    purpose: FilePurpose,
    payload: bytes,
    code: str,
    status: int,
    message_part: str,
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a), pytest.raises(AppError) as caught:
        await files.store_upload(purpose, payload, "upload")

    assert (caught.value.code, caught.value.status_code) == (code, status)
    assert message_part in caught.value.message
    assert fake_file_storage.keys() == []
    assert await _rows(tenant_a) == []


async def test_missing_encryption_key_refuses_files(
    two_tenants: Tenants,
    fake_file_storage: FakeFileStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    with tenant_context(tenant_a), pytest.raises(AppError) as caught:
        await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, "a.pdf")

    assert (caught.value.code, caught.value.status_code) == ("ERR-FILES-005", 503)
    assert fake_file_storage.keys() == []


async def test_row_without_object_is_a_storage_failure_not_404(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    """Том не смонтирован или восстановлена БД без тома: это авария (ERR-FILES-006)."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        stored = await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, "a.pdf")
        for key in list(fake_file_storage.keys()):
            await fake_file_storage.delete(key)
        with pytest.raises(AppError) as caught:
            await files.read_file(stored.id)

    assert (caught.value.code, caught.value.status_code) == ("ERR-FILES-006", 503)


async def test_delete_removes_object_and_row(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        stored = await files.store_upload(FilePurpose.MENU_PHOTO, make_png(), "plov.jpg")
        await files.delete_file(stored.id)

    assert fake_file_storage.keys() == []
    assert await _rows(tenant_a) == []


async def test_delete_one_of_two_keeps_the_other(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    """Удаление бьёт ровно по своему id: «Удалить» без WHERE стёрло бы все файлы
    тенанта (ревью PR #495, Н-3)."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        first = await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, "first.pdf")
        second = await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF + b"%2", "second.pdf")
        await files.delete_file(first.id)
        survivor = await files.read_file(second.id)

    assert survivor.data == PDF + b"%2"
    assert [row.id for row in await _rows(tenant_a)] == [second.id]
    assert len(fake_file_storage.keys()) == 1


async def test_failed_row_insert_removes_the_stored_object(
    two_tenants: Tenants,
    fake_file_storage: FakeFileStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Порядок «файл, потом строка»: упала вставка строки — объект удалён тут же."""
    tenant_a, _ = two_tenants

    def broken_scope() -> object:
        raise RuntimeError("database is down")

    monkeypatch.setattr(files, "session_scope", broken_scope)
    with tenant_context(tenant_a), pytest.raises(RuntimeError):
        await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, "rekv.pdf")

    assert fake_file_storage.keys() == []


async def test_file_response_headers(
    two_tenants: Tenants, fake_file_storage: FakeFileStorageAdapter
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        document = await files.read_file(
            (await files.store_upload(FilePurpose.GUEST_DOCUMENT, PDF, 'Счёт "1".pdf')).id
        )
        photo = await files.read_file(
            (await files.store_upload(FilePurpose.MENU_PHOTO, make_png(), "plov.png")).id
        )

    document_response = files.file_response(document)
    photo_response = files.file_response(photo)

    assert document_response.headers["content-type"] == "application/pdf"
    assert document_response.headers["content-disposition"] == (
        'attachment; filename="____ _1_.pdf"; '
        "filename*=UTF-8''%D0%A1%D1%87%D1%91%D1%82%20%221%22.pdf"
    )
    assert document_response.headers["cache-control"] == "no-store"
    assert photo_response.headers["content-type"] == "image/webp"
    assert "content-disposition" not in photo_response.headers
    assert photo_response.headers["cache-control"] == "private, max-age=3600"
    for response in (document_response, photo_response):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]


async def test_sandbox_failure_is_a_storage_failure_and_stores_nothing(
    two_tenants: Tenants,
    fake_file_storage: FakeFileStorageAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, _ = two_tenants
    monkeypatch.setattr(sys, "executable", "/usr/bin/false")

    with tenant_context(tenant_a), pytest.raises(AppError) as caught:
        await files.store_upload(FilePurpose.MENU_PHOTO, make_png(), "plov.png")

    assert (caught.value.code, caught.value.status_code) == ("ERR-FILES-006", 503)
    assert fake_file_storage.keys() == []
    assert await _rows(tenant_a) == []
