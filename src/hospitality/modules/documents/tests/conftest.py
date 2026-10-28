"""Фикстуры тестов модуля documents (spec 0049) — канон
`modules/housekeeping/tests/conftest.py`: общие DB-фикстуры реимпортируются из
`tests/conftest.py`.

F811 отключён на файл: фикстура-параметр (`canonical_database`) обязана
называться как реимпортированная фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import io
import uuid
import zipfile
from dataclasses import dataclass
from datetime import timedelta

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hospitality.modules.documents.api import ActingUser, ActRequestSubmit
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import Tenant, User
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    fake_file_storage,
    migrated_database_name,
)


@dataclass(frozen=True)
class Hotel:
    tenant_id: uuid.UUID
    stay_id: uuid.UUID
    accounting_id: uuid.UUID


@dataclass(frozen=True)
class Hotels:
    a: Hotel
    b: Hotel
    storage: FileStoragePort


async def _hotel(slug: str, room: str) -> Hotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug.title())
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    with tenant_context(tenant_id):
        category = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="accounting", name="Бухгалтерия")
        )
        checked_in = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + timedelta(days=2))
        )
    return Hotel(tenant_id, checked_in.stay.id, category.id)


@pytest.fixture
async def hotels(canonical_database: None, fake_file_storage: FileStoragePort) -> Hotels:
    """Два отеля со службой «Бухгалтерия» и заселённым гостем; ключ файлов задан."""
    return Hotels(await _hotel("hotel-a", "512"), await _hotel("hotel-b", "101"), fake_file_storage)


def submit_form(
    hotel: Hotel,
    *,
    client_key: str | None = None,
    bill_to: str = "",
    note: str = "",
    language: str = "ru",
) -> ActRequestSubmit:
    return ActRequestSubmit.model_validate(
        {
            "stay_id": hotel.stay_id,
            "guest_identity_id": uuid.uuid4(),
            "client_key": client_key or uuid.uuid4().hex,
            "language": language,
            "bill_to": bill_to,
            "note": note,
        }
    )


async def make_acting_user(display_name: str) -> ActingUser:
    """Платформенный User: `checked_by_user_id` — FK на `users`."""
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        return ActingUser(user_id=user.id, display_name=display_name)


def make_text_pdf(lines: list[str]) -> bytes:
    """PDF с текстовым слоем: Helvetica, по строке на `Td` (только латиница)."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    body = "".join(f"({line}) Tj 0 -16 Td " for line in lines)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 11 Tf 50 800 Td {body}ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def make_blank_pdf() -> bytes:
    """«Скан»: страница без текстового слоя."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_requisites_docx(rows: list[tuple[str, str]], *, doctype: str = "") -> bytes:
    """DOCX с таблицей «метка | значение» — как карточка предприятия."""
    cells = "".join(
        f"<w:tr><w:tc><w:p><w:r><w:t>{label}</w:t></w:r></w:p></w:tc>"
        f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc></w:tr>"
        for label, value in rows
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>{doctype}'
        f'<w:document xmlns:w="{_W}"><w:body><w:tbl>{cells}</w:tbl></w:body></w:document>'
    ).encode()
    content_types = (
        b'<?xml version="1.0"?><Types><Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
        b'document.main+xml"/></Types>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return out.getvalue()
