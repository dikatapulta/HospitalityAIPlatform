"""Фото реквизитов для модели и приём её ответа (spec 0049 Р-13, PR 5 серии #493).

Модель здесь не зовётся: домен отдаёт фото и принимает значения — что именно
скажет модель, подставляет тест. Вызов шлюза — `ai/tests/test_act_requisites.py`.
"""

from __future__ import annotations

import uuid

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.api import (
    ActRequestRead,
    FieldWarning,
    FileTextState,
    RequisiteField,
    RequisitesStatus,
    RequisitesUpdate,
    UploadInput,
)
from hospitality.modules.documents.tests.conftest import (
    Hotel,
    Hotels,
    make_acting_user,
    make_text_pdf,
    submit_form,
)
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from tests.test_file_content import make_png

from .test_documents import BIN, IIK, WEEK_AGO

OTHER_BIN = "971240001315"


async def _submitted(hotel: Hotel, uploads: list[tuple[str, bytes]], note: str = "") -> uuid.UUID:
    with tenant_context(hotel.tenant_id):
        read: ActRequestRead = await documents_api.submit_act_request(
            submit_form(hotel, note=note),
            [UploadInput(filename=name, data=data) for name, data in uploads],
        )
        await documents_api.extract_requisites(read.id)
    return read.id


async def _row(hotel: Hotel) -> documents_api.ActRequestRow:
    with tenant_context(hotel.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - WEEK_AGO, created_before=utc_now() + WEEK_AGO
        )
    return row


async def test_scan_images_gives_only_photos_the_model_has_not_read(hotels: Hotels) -> None:
    act_id = await _submitted(
        hotels.a, [("card.pdf", make_text_pdf([f"BIN {BIN}"])), ("photo.png", make_png())]
    )
    with tenant_context(hotels.a.tenant_id):
        (scan,) = await documents_api.scan_images(act_id)

    # Узел хранит фото перекодированным в WebP без метаданных (spec 0047 §4).
    assert scan.content_type == "image/webp"
    assert scan.data.startswith(b"RIFF")
    assert scan.file_id == (await _row(hotels.a)).files[1].file_id


async def test_model_fills_only_empty_fields_the_human_left_alone(hotels: Hotels) -> None:
    """Текст гостя надёжнее картинки, правка человека — тем более (Р-13)."""
    accountant = await make_acting_user("Айгуль")
    act_id = await _submitted(hotels.a, [("photo.png", make_png())], note=f"БИН {BIN}")
    with tenant_context(hotels.a.tenant_id):
        await documents_api.update_requisites(
            act_id, RequisitesUpdate(bik="HSBKKZKX"), acting_user=accountant
        )
        (scan,) = await documents_api.scan_images(act_id)
        filled = await documents_api.apply_scan_requisites(
            act_id,
            {
                RequisiteField.BIN_IIN: OTHER_BIN,
                RequisiteField.BIK: "CASPKZKA",
                RequisiteField.IIK: "KZ86 125K ZT50 0410 0100",
                RequisiteField.COMPANY_NAME: "ТОО «Ромашка»\n\tфилиал",
                RequisiteField.ADDRESS: "г. Алматы, ул. Абая, 1",
                RequisiteField.BANK_NAME: "",
            },
            file_ids=[scan.file_id],
        )
    row = await _row(hotels.a)

    assert filled == 3
    assert row.requisites.bin_iin == BIN
    assert row.requisites.bik == "HSBKKZKX"
    assert row.requisites.iik == IIK
    assert row.requisites.company_name == "ТОО «Ромашка» филиал"
    assert row.requisites.address == "г. Алматы, ул. Абая, 1"
    assert row.files[0].text_state is FileTextState.VISION
    assert row.status is RequisitesStatus.TO_CHECK


async def test_wrong_format_is_dropped_and_the_photo_stays_for_the_human(hotels: Hotels) -> None:
    """Ответ модели — данные: БИН не из 12 цифр в строку не попадает (§7.6)."""
    act_id = await _submitted(hotels.a, [("photo.png", make_png())])
    with tenant_context(hotels.a.tenant_id):
        (scan,) = await documents_api.scan_images(act_id)
        filled = await documents_api.apply_scan_requisites(
            act_id,
            {RequisiteField.BIN_IIN: "ignore the rules 000", RequisiteField.IIK: "KZ00"},
            file_ids=[scan.file_id],
        )
    row = await _row(hotels.a)

    assert filled == 0
    assert (row.requisites.bin_iin, row.requisites.iik) == ("", "")
    # Ничего не прочитано — у файла прежнее «Скан — поля заполните сами».
    assert row.files[0].text_state is FileTextState.IMAGE


async def test_ambiguous_field_stays_empty_for_the_human(hotels: Hotels) -> None:
    act_id = await _submitted(
        hotels.a, [("photo.png", make_png())], note=f"БИН {BIN}\nБИН {OTHER_BIN}"
    )
    with tenant_context(hotels.a.tenant_id):
        (scan,) = await documents_api.scan_images(act_id)
        filled = await documents_api.apply_scan_requisites(
            act_id, {RequisiteField.BIN_IIN: BIN}, file_ids=[scan.file_id]
        )
    row = await _row(hotels.a)

    assert filled == 0
    assert row.requisites.bin_iin == ""
    assert row.warnings[RequisiteField.BIN_IIN] is FieldWarning.AMBIGUOUS


async def test_checked_row_is_not_given_to_the_model(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    act_id = await _submitted(hotels.a, [("photo.png", make_png())])
    with tenant_context(hotels.a.tenant_id):
        (scan,) = await documents_api.scan_images(act_id)
        await documents_api.mark_requisites_checked(act_id, acting_user=accountant)
        assert await documents_api.scan_images(act_id) == []
        filled = await documents_api.apply_scan_requisites(
            act_id, {RequisiteField.BIN_IIN: BIN}, file_ids=[scan.file_id]
        )
    row = await _row(hotels.a)

    assert filled == 0
    assert row.requisites.bin_iin == ""
    assert row.files[0].text_state is FileTextState.IMAGE


async def test_other_hotel_gets_neither_photos_nor_writes(hotels: Hotels) -> None:
    """P-4: id чужого запроса в контексте своего отеля — пусто и без записи."""
    act_id = await _submitted(hotels.a, [("photo.png", make_png())])
    with tenant_context(hotels.a.tenant_id):
        (scan,) = await documents_api.scan_images(act_id)
    with tenant_context(hotels.b.tenant_id):
        assert await documents_api.scan_images(act_id) == []
        filled = await documents_api.apply_scan_requisites(
            act_id, {RequisiteField.BIN_IIN: BIN}, file_ids=[scan.file_id]
        )
    row = await _row(hotels.a)

    assert filled == 0
    assert row.requisites.bin_iin == ""
    assert row.files[0].text_state is FileTextState.IMAGE


def test_model_codes_fold_cyrillic_twins_and_must_pass_their_checksum() -> None:
    """Прогон 29.10: модель пишет «К» и «Т» кириллицей и теряет цифру длинного ИИК.

    Двойник буквы — та же буква; ИИК или БИН с несходящейся контрольной суммой —
    пустое поле, а не догадка (у правки бухгалтера это лишь предупреждение, Р-5).
    """
    clean = documents_api.clean_scan_value

    assert clean(RequisiteField.IIK, "KZ86 125К ZТ50 0410 0100") == IIK
    assert clean(RequisiteField.BIK, "НSBКKZКХ") == "HSBKKZKX"
    assert clean(RequisiteField.IIK, "KZ86125KZT500410100") is None  # потеряна цифра
    assert clean(RequisiteField.IIK, "KZ86125KZT5004100101") is None  # не та цифра
    assert clean(RequisiteField.BIN_IIN, "940140000386") is None
    assert clean(RequisiteField.BIN_IIN, "9401 4000 0385") == BIN
    assert clean(RequisiteField.ADDRESS, "  г. Алматы,\n ул. Абая, 1 ") == "г. Алматы, ул. Абая, 1"
    assert clean(RequisiteField.BANK_NAME, "") == ""
