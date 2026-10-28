"""Разбор реквизитов: ошибка файла против ошибки среды (issue #511, spec 0049 Р-4).

Ошибка одного файла — вердикт о нём: у файла `failed`, остальные файлы и текст
гостя разбираются. Ошибка среды (том, песочница, ключ) — исключение без
записи: outbox повторит, а исчерпав повторы, расскажет человеку алертом.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents import processing
from hospitality.modules.documents.api import ActRequestRead, ExtractionState, FileTextState
from hospitality.modules.documents.models import ActRequestFile
from hospitality.modules.documents.tests.conftest import (
    Hotel,
    Hotels,
    make_text_pdf,
    submit_form,
)
from hospitality.platform.file_sandbox import SandboxFailedError, extract_text_isolated
from hospitality.platform.file_text import ExtractedText
from hospitality.platform.files import ERR_FILE_KEY, ERR_FILE_STORAGE, UploadedFile
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

BIN = "940140000385"
GOOD_PDF = make_text_pdf([f"BIN {BIN}"])
OTHER_PDF = make_text_pdf(["BIK HSBKKZKX"])
WEEK = timedelta(days=7)


async def _submit(hotel: Hotel, *pdfs: bytes, note: str = "") -> ActRequestRead:
    uploads = [
        documents_api.UploadInput(filename=f"card-{index}.pdf", data=pdf)
        for index, pdf in enumerate(pdfs)
    ]
    with tenant_context(hotel.tenant_id):
        return await documents_api.submit_act_request(submit_form(hotel, note=note), uploads)


async def _storage_keys(hotel: Hotel, act_request_id: uuid.UUID) -> list[str]:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            return list(
                await session.scalars(
                    select(UploadedFile.storage_key)
                    .join(ActRequestFile, ActRequestFile.file_id == UploadedFile.id)
                    .where(ActRequestFile.act_request_id == act_request_id)
                    .order_by(ActRequestFile.position)
                )
            )


async def _row(hotel: Hotel) -> documents_api.ActRequestRow:
    with tenant_context(hotel.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - WEEK, created_before=utc_now() + WEEK
        )
    return row


async def _extract(hotel: Hotel, read: ActRequestRead) -> None:
    with tenant_context(hotel.tenant_id):
        await documents_api.extract_requisites(read.id)


async def test_missing_object_on_healthy_storage_fails_only_that_file(hotels: Hotels) -> None:
    """Объекта нет, а том исправен — вердикт о файле, а не 10 повторов и dead-letter."""
    read = await _submit(hotels.a, GOOD_PDF, OTHER_PDF, note="БИН " + BIN)
    first_key, _ = await _storage_keys(hotels.a, read.id)
    await hotels.storage.delete(first_key)

    await _extract(hotels.a, read)
    row = await _row(hotels.a)

    assert [file.text_state for file in row.files] == [FileTextState.FAILED, FileTextState.TEXT]
    assert row.extraction_state is ExtractionState.FAILED
    assert (row.requisites.bin_iin, row.requisites.bik) == (BIN, "HSBKKZKX")


async def test_undecryptable_file_fails_only_that_file(hotels: Hotels) -> None:
    read = await _submit(hotels.a, GOOD_PDF, OTHER_PDF)
    first_key, _ = await _storage_keys(hotels.a, read.id)
    await hotels.storage.put(first_key, b"not a ciphertext", "application/octet-stream")

    await _extract(hotels.a, read)
    row = await _row(hotels.a)

    assert [file.text_state for file in row.files] == [FileTextState.FAILED, FileTextState.TEXT]
    assert row.requisites.bik == "HSBKKZKX"


async def test_storage_down_is_not_a_verdict(
    hotels: Hotels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Том не проходит проверку — исключение, outbox повторит; ничего не записано."""
    read = await _submit(hotels.a, GOOD_PDF)
    (key,) = await _storage_keys(hotels.a, read.id)
    await hotels.storage.delete(key)

    async def storage_down() -> bool:
        return False

    monkeypatch.setattr(hotels.storage, "check", storage_down)
    with pytest.raises(AppError) as caught:
        await _extract(hotels.a, read)
    row = await _row(hotels.a)

    assert caught.value.code == ERR_FILE_STORAGE
    assert row.extraction_state is ExtractionState.PENDING
    assert row.files[0].text_state is FileTextState.PENDING


async def test_missing_key_is_not_a_verdict(hotels: Hotels) -> None:
    """Ключа нет совсем — файлы не получают `failed`: запись полей падает целиком."""
    read = await _submit(hotels.a, GOOD_PDF)
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("FILE_ENCRYPTION_KEY")
        get_settings.cache_clear()
        try:
            with pytest.raises(AppError) as caught:
                await _extract(hotels.a, read)
        finally:
            get_settings.cache_clear()
    row = await _row(hotels.a)

    assert caught.value.code == ERR_FILE_KEY
    assert row.extraction_state is ExtractionState.PENDING
    assert row.files[0].text_state is FileTextState.PENDING


async def test_child_killed_by_a_file_fails_only_that_file(
    hotels: Hotels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ребёнка убил лимит на этом файле, а проба на пустом входе отвечает — вердикт."""
    real_extract = extract_text_isolated

    async def killed_on_good_pdf(data: bytes) -> ExtractedText:
        if data == GOOD_PDF:
            raise SandboxFailedError("exit -24: ")
        return await real_extract(data)

    monkeypatch.setattr(processing, "extract_text_isolated", killed_on_good_pdf)
    read = await _submit(hotels.a, GOOD_PDF, OTHER_PDF)

    await _extract(hotels.a, read)
    row = await _row(hotels.a)

    assert [file.text_state for file in row.files] == [FileTextState.FAILED, FileTextState.TEXT]
    assert row.requisites.bik == "HSBKKZKX"


async def test_broken_sandbox_is_not_a_verdict(
    hotels: Hotels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Песочница не отвечает и на пустом входе — среда сломана: исключение, а не
    «Не разобран» у каждого файла молча."""
    read = await _submit(hotels.a, GOOD_PDF)
    monkeypatch.setattr(sys, "executable", "/usr/bin/false")

    with pytest.raises(SandboxFailedError):
        await _extract(hotels.a, read)
    row = await _row(hotels.a)

    assert row.extraction_state is ExtractionState.PENDING
    assert row.files[0].text_state is FileTextState.PENDING
