"""Модуль documents: приём, шифрование, разбор, правка, обезличивание (spec 0049 §7 PR 2).

Живой Postgres (временная БД с миграциями), Fake-хранилище в порту и тестовый
ключ файлов. Разбор идёт через настоящий дочерний процесс песочницы.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents import service
from hospitality.modules.documents.api import (
    ActRequestRead,
    ExtractionState,
    FieldWarning,
    FileTextState,
    RequisiteField,
    Requisites,
    RequisitesStatus,
    RequisitesUpdate,
    UploadInput,
)
from hospitality.modules.documents.models import ActRequest
from hospitality.modules.documents.tests.conftest import (
    Hotel,
    Hotels,
    make_acting_user,
    make_blank_pdf,
    make_requisites_docx,
    make_text_pdf,
    submit_form,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform import files
from hospitality.platform.file_content import FilePurpose
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context
from tests.test_file_content import make_png

BIN = "940140000385"
IIK = "KZ86125KZT5004100100"
CARD_PDF_LINES = [f"BIN {BIN}", "IIK KZ86 125K ZT50 0410 0100", "BIK HSBKKZKX"]
WEEK_AGO = timedelta(days=7)


async def _submit(
    hotel: Hotel, uploads: list[tuple[str, bytes]] | None = None, **form: Any
) -> ActRequestRead:
    with tenant_context(hotel.tenant_id):
        return await documents_api.submit_act_request(
            submit_form(hotel, **form),
            [UploadInput(filename=name, data=data) for name, data in uploads or []],
        )


async def _rows(hotel: Hotel) -> list[documents_api.ActRequestRow]:
    with tenant_context(hotel.tenant_id):
        return await documents_api.list_act_requests(
            created_from=utc_now() - WEEK_AGO, created_before=utc_now() + WEEK_AGO
        )


async def _count(hotel: Hotel, table: str) -> int:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            return int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


async def test_submit_stores_ciphertext_and_creates_accounting_request(hotels: Hotels) -> None:
    read = await _submit(
        hotels.a,
        [("Реквизиты.pdf", make_text_pdf(CARD_PDF_LINES))],
        bill_to="ТОО «Ромашка»",
        note="Счёт-фактуру тоже, пожалуйста",
        language="kk",
    )

    with tenant_context(hotels.a.tenant_id):
        async with session_scope() as session:
            raw = (
                await session.execute(
                    text("SELECT guest_input_ciphertext, room_number FROM act_requests")
                )
            ).one()
            events = list(
                await session.scalars(
                    select(OutboxEvent).where(OutboxEvent.event_name == "act_request.submitted")
                )
            )
            service_day = await session.scalar(text("SELECT service_day FROM service_requests"))
        request = await requests_api.get_request(read.request_id)
        summary = await requests_api.day_summary(service_day)

    assert raw.room_number == "512"
    assert "Ромашка".encode() not in raw.guest_input_ciphertext
    assert read.files_count == 1
    assert request.origin is requests_api.ServiceRequestOrigin.GUEST_FORM
    assert request.category_id == hotels.a.accounting_id
    assert (request.summary, request.details) == (documents_api.REQUEST_SUMMARY, "Файлов: 1")
    assert (request.room_number, request.guest_language) == ("512", "kk")
    assert len(events) == 1
    assert "Ромашка" not in str(events[0].payload)
    assert summary.created_by_origin[requests_api.ServiceRequestOrigin.GUEST_FORM] == 1


async def test_extraction_fills_fields_from_text_pdf_and_docx(hotels: Hotels) -> None:
    read = await _submit(
        hotels.a,
        [
            ("card.pdf", make_text_pdf(CARD_PDF_LINES)),
            ("card.docx", make_requisites_docx([("Банк", "АО «Kaspi Bank»")])),
            ("scan.pdf", make_blank_pdf()),
            ("photo.png", make_png()),
        ],
        note="Юридический адрес: г. Алматы, ул. Абая, 1",
    )
    with tenant_context(hotels.a.tenant_id):
        await documents_api.extract_requisites(read.id)
    (row,) = await _rows(hotels.a)

    assert row.requisites == Requisites(
        bin_iin=BIN,
        iik=IIK,
        bik="HSBKKZKX",
        bank_name="АО «Kaspi Bank»",
        address="г. Алматы, ул. Абая, 1",
    )
    assert [file.text_state for file in row.files] == [
        FileTextState.TEXT,
        FileTextState.TEXT,
        FileTextState.NO_TEXT,
        FileTextState.IMAGE,
    ]
    assert row.extraction_state is ExtractionState.DONE
    assert row.status is RequisitesStatus.TO_CHECK
    assert row.note == "Юридический адрес: г. Алматы, ул. Абая, 1"


async def test_unreadable_docx_marks_file_failed_and_keeps_the_request(hotels: Hotels) -> None:
    dtd = '<!DOCTYPE x [<!ENTITY a "a">]>'
    read = await _submit(
        hotels.a, [("bad.docx", make_requisites_docx([("БИН", BIN)], doctype=dtd))]
    )
    with tenant_context(hotels.a.tenant_id):
        await documents_api.extract_requisites(read.id)
    (row,) = await _rows(hotels.a)

    assert row.files[0].text_state is FileTextState.FAILED
    assert row.extraction_state is ExtractionState.FAILED
    assert row.requisites.bin_iin == ""


async def test_human_edit_survives_repeated_extraction_and_unchecks(hotels: Hotels) -> None:
    """Н-10 ревью PR #500: отметка «правил человек» — `edited_fields`; повтор
    разбора правленое не трогает, правка снимает «Проверено»."""
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels.a, note=f"БИН {BIN}\nБИК HSBKKZKX")
    with tenant_context(hotels.a.tenant_id):
        await documents_api.extract_requisites(read.id)
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
        edited = await documents_api.update_requisites(
            read.id, RequisitesUpdate(bin_iin="971240001315"), acting_user=accountant
        )
        await documents_api.extract_requisites(read.id)
    (row,) = await _rows(hotels.a)

    assert edited.status is RequisitesStatus.TO_CHECK
    assert row.edited_fields == [RequisiteField.BIN_IIN]
    assert row.requisites.bin_iin == "971240001315"
    assert row.requisites.bik == "HSBKKZKX"


async def test_checked_row_is_left_alone_by_extraction(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels.a, note=f"БИН {BIN}")
    with tenant_context(hotels.a.tenant_id):
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
        await documents_api.extract_requisites(read.id)
    (row,) = await _rows(hotels.a)

    assert row.status is RequisitesStatus.CHECKED
    assert row.checked_by_display_name == "Айгуль"
    assert row.requisites.bin_iin == ""


async def test_update_rejects_wrong_format_and_warns_on_checksum(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels.a, bill_to="ИП Иванов")
    with tenant_context(hotels.a.tenant_id):
        with pytest.raises(AppError) as rejected:
            await documents_api.update_requisites(
                read.id, RequisitesUpdate(iik="KZ00"), acting_user=accountant
            )
        warned = await documents_api.update_requisites(
            read.id, RequisitesUpdate(bin_iin="940140000386"), acting_user=accountant
        )

    assert rejected.value.code == documents_api.ERR_DOCUMENTS_FIELD_FORMAT
    assert warned.requisites.bin_iin == "940140000386"
    assert warned.warnings == {RequisiteField.BIN_IIN: FieldWarning.CHECKSUM}


async def test_foreign_aad_does_not_decrypt(hotels: Hotels) -> None:
    """Шифртекст, переложенный в другую строку, не расшифруется (Р-3)."""
    first = await _submit(hotels.a, bill_to="ТОО Первая")
    await _submit(hotels.a, bill_to="ТОО Вторая")
    with tenant_context(hotels.a.tenant_id):
        async with session_scope() as session:
            await session.execute(
                text(
                    "UPDATE act_requests SET guest_input_ciphertext = "
                    "(SELECT guest_input_ciphertext FROM act_requests WHERE id = :id) "
                    "WHERE id <> :id"
                ),
                {"id": first.id},
            )
    with pytest.raises(AppError) as unavailable:
        await _rows(hotels.a)

    assert unavailable.value.code == "ERR-FILES-005"


async def test_without_key_nothing_is_stored(hotels: Hotels) -> None:
    # Свой контекст, а не общий `monkeypatch`: его откат вернул бы и POSTGRES_DB.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FILE_ENCRYPTION_KEY", "")
        get_settings.cache_clear()
        with pytest.raises(AppError) as unavailable:
            await _submit(hotels.a, [("card.pdf", make_text_pdf(CARD_PDF_LINES))], bill_to="ТОО")
    get_settings.cache_clear()

    assert unavailable.value.code == "ERR-FILES-005"
    assert await _count(hotels.a, "act_requests") == 0
    assert await _count(hotels.a, "uploaded_files") == 0


async def test_same_client_key_is_one_request_and_one_service_request(hotels: Hotels) -> None:
    first = await _submit(hotels.a, client_key="key-00000001", bill_to="ТОО Ромашка")
    second = await _submit(
        hotels.a,
        [("card.pdf", make_text_pdf(CARD_PDF_LINES))],
        client_key="key-00000001",
        bill_to="ТОО Ромашка",
    )

    assert second.id == first.id
    assert await _count(hotels.a, "service_requests") == 1
    assert await _count(hotels.a, "uploaded_files") == 0


async def test_retry_with_same_client_key_creates_the_missing_service_request(
    hotels: Hotels,
) -> None:
    """Процесс упал между строкой и заявкой (spec 0049 Р-1, spec 0038 Р-3): повтор
    тем же `client_key` доводит заявку — одна строка, одна заявка, файлы первой отправки."""

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("process died")

    pdf = ("card.pdf", make_text_pdf(CARD_PDF_LINES))
    with pytest.MonkeyPatch.context() as patch, pytest.raises(RuntimeError):
        patch.setattr(requests_api, "create_request", crash)
        await _submit(hotels.a, [pdf, pdf], client_key="key-00000003", bill_to="ТОО Ромашка")
    retried = await _submit(hotels.a, [pdf], client_key="key-00000003", bill_to="ТОО Ромашка")
    with tenant_context(hotels.a.tenant_id):
        async with session_scope() as session:
            rows = list(await session.scalars(select(ActRequest)))
            details = list(await session.scalars(text("SELECT details FROM service_requests")))

    assert [(row.id, row.request_id, row.request_created) for row in rows] == [
        (retried.id, retried.request_id, True)
    ]
    assert details == ["Файлов: 2"]
    assert retried.files_count == 2
    assert await _count(hotels.a, "uploaded_files") == 2


async def test_race_loser_deletes_its_files_and_gets_the_winner(
    hotels: Hotels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обе отправки прошли проверку ключа до вставки: UNIQUE пускает одну,
    проигравший удаляет свои файлы и отдаёт запрос победителя."""
    winner = await _submit(hotels.a, client_key="key-00000002", bill_to="ТОО Ромашка")
    original = service._find_by_client_key
    calls = 0

    async def miss_first(stay_id: uuid.UUID, client_key: str) -> ActRequest | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await original(stay_id, client_key)

    monkeypatch.setattr(service, "_find_by_client_key", miss_first)
    loser = await _submit(
        hotels.a, [("card.pdf", make_text_pdf(CARD_PDF_LINES))], client_key="key-00000002"
    )

    assert loser.id == winner.id
    assert await _count(hotels.a, "act_requests") == 1
    assert await _count(hotels.a, "service_requests") == 1
    assert await _count(hotels.a, "uploaded_files") == 0


async def test_limits_empty_form_and_missing_accounting(hotels: Hotels) -> None:
    pdf = ("card.pdf", make_text_pdf(CARD_PDF_LINES))
    with pytest.raises(AppError) as empty:
        await _submit(hotels.a, bill_to="   ")
    with pytest.raises(AppError) as six_files:
        await _submit(hotels.a, [pdf] * 6)
    for index in range(documents_api.MAX_REQUESTS_PER_STAY):
        await _submit(hotels.a, bill_to=f"ТОО {index}")
    with pytest.raises(AppError) as sixth_request:
        await _submit(hotels.a, bill_to="ТОО шестая")
    with tenant_context(hotels.b.tenant_id):
        async with session_scope() as session:
            await session.execute(text("DELETE FROM request_categories"))
        accepts = await documents_api.accepts_act_requests()
    with pytest.raises(AppError) as disabled:
        await _submit(hotels.b, bill_to="ТОО")

    assert (empty.value.code, empty.value.status_code) == ("ERR-DOCUMENTS-001", 422)
    assert (six_files.value.code, six_files.value.status_code) == ("ERR-DOCUMENTS-002", 413)
    assert (sixth_request.value.code, sixth_request.value.status_code) == (
        "ERR-DOCUMENTS-003",
        429,
    )
    assert accepts is False
    assert (disabled.value.code, disabled.value.status_code) == ("ERR-DOCUMENTS-005", 404)


async def test_rejected_file_removes_files_already_accepted(hotels: Hotels) -> None:
    good = ("card.pdf", make_text_pdf(CARD_PDF_LINES))
    with pytest.raises(AppError) as rejected:
        await _submit(hotels.a, [good, good, ("evil.html", b"<html>")])

    assert rejected.value.code == "ERR-FILES-002"
    assert await _count(hotels.a, "uploaded_files") == 0
    assert await _count(hotels.a, "act_requests") == 0
    # Fake-адаптер умеет перечислить объекты, порт — нет; модулю адаптер
    # импортировать нельзя (контракт 2 import-linter).
    assert cast(Any, hotels.storage).keys() == []


async def test_tenant_isolation_of_rows_and_files(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels.a, [("card.pdf", make_text_pdf(CARD_PDF_LINES))], bill_to="ТОО")
    with tenant_context(hotels.a.tenant_id):
        (own,) = await documents_api.list_act_requests(
            created_from=utc_now() - WEEK_AGO, created_before=utc_now() + WEEK_AGO
        )
        file_id = own.files[0].file_id
        assert (await documents_api.read_act_request_file(read.id, file_id)).data
    with tenant_context(hotels.b.tenant_id):
        foreign_by_request = await documents_api.get_act_request_by_request(read.request_id)
        attempts = [
            documents_api.read_act_request_file(read.id, file_id),
            documents_api.update_requisites(
                read.id, RequisitesUpdate(bin_iin=BIN), acting_user=accountant
            ),
            documents_api.mark_requisites_checked(read.id, acting_user=accountant),
        ]
        codes = []
        for attempt in attempts:
            with pytest.raises(AppError) as refused:
                await attempt
            codes.append(refused.value.code)
        async with session_scope() as session:
            touched = await session.execute(text("UPDATE act_requests SET room_number = 'x'"))
            await session.execute(text("UPDATE act_request_files SET position = 9"))

    assert foreign_by_request is None
    assert codes == ["ERR-DOCUMENTS-004"] * 3
    assert touched.rowcount == 0  # type: ignore[attr-defined]
    assert await _rows(hotels.b) == []
    assert await _count(hotels.b, "act_request_files") == 0
    assert (await _rows(hotels.a))[0].room_number == "512"


async def test_file_of_another_request_is_not_served(hotels: Hotels) -> None:
    first = await _submit(hotels.a, [("a.pdf", make_text_pdf(CARD_PDF_LINES))])
    second = await _submit(hotels.a, [("b.pdf", make_text_pdf(CARD_PDF_LINES))])
    rows = {row.id: row for row in await _rows(hotels.a)}
    with tenant_context(hotels.a.tenant_id), pytest.raises(AppError) as refused:
        await documents_api.read_act_request_file(first.id, rows[second.id].files[0].file_id)

    assert refused.value.code == "ERR-DOCUMENTS-004"


async def test_anonymize_erases_ciphertexts_and_keeps_the_row(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels.a, bill_to="ТОО Ромашка", note=f"БИН {BIN}")
    with tenant_context(hotels.a.tenant_id):
        await documents_api.extract_requisites(read.id)
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
        first = await documents_api.anonymize_expired_act_requests(
            created_before=utc_now() + timedelta(seconds=1)
        )
        second = await documents_api.anonymize_expired_act_requests(
            created_before=utc_now() + timedelta(seconds=1)
        )
        async with session_scope() as session:
            raw = (
                await session.execute(
                    text(
                        "SELECT guest_input_ciphertext, requisites_ciphertext, "
                        "checked_by_display_name FROM act_requests"
                    )
                )
            ).one()
        with pytest.raises(AppError) as gone:
            await documents_api.update_requisites(
                read.id, RequisitesUpdate(bin_iin=BIN), acting_user=accountant
            )
    (row,) = await _rows(hotels.a)

    assert (first, second) == (1, 0)
    assert tuple(raw) == (None, None, None)
    assert (row.anonymized, row.bill_to, row.requisites) == (True, "", Requisites())
    assert row.room_number == "512"
    assert gone.value.code == "ERR-DOCUMENTS-004"


async def test_abandoned_request_without_service_request_is_deleted_with_files(
    hotels: Hotels,
) -> None:
    """Сбой между строкой и заявкой (spec 0038 Р-3): брошенную строку снимает
    ретеншн через сутки; строку, чья заявка всё-таки создалась, — только отмечает."""

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("process died")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(RuntimeError):
        patch.setattr(requests_api, "create_request", crash)
        await _submit(hotels.a, [("a.pdf", make_text_pdf(CARD_PDF_LINES))], bill_to="ТОО")
    healed = await _submit(hotels.a, bill_to="ТОО вторая")
    with tenant_context(hotels.a.tenant_id):
        async with session_scope() as session:
            await session.execute(text("UPDATE act_requests SET request_created = false"))
        deleted = await documents_api.delete_abandoned_act_requests(
            created_before=utc_now() + timedelta(seconds=1)
        )
        async with session_scope() as session:
            left = list(await session.scalars(select(ActRequest)))
            files_left = await session.scalar(select(func.count()).select_from(files.UploadedFile))

    assert deleted == 1
    assert [(row.id, row.request_created) for row in left] == [(healed.id, True)]
    assert files_left == 0


async def test_guest_document_purpose_is_used(hotels: Hotels) -> None:
    await _submit(hotels.a, [("a.pdf", make_text_pdf(CARD_PDF_LINES))])
    with tenant_context(hotels.a.tenant_id):
        async with session_scope() as session:
            purposes = list(await session.scalars(select(files.UploadedFile.purpose)))

    assert purposes == [FilePurpose.GUEST_DOCUMENT.value]
