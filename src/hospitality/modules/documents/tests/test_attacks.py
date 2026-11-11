"""Набор атак по пути реквизитов без модели: форма → песочница → строка (issue #954).

Текст и файлы гостя — недоверенный ввод (FOUNDATION §7.6): инструкция в них
ничего не исполняет, её только сравнивают с шаблонами §4. Каждая атака набора
(`attacks.py`) проходит настоящий путь — приём формы, разбор PDF и DOCX в
дочернем процессе песочницы, запись полей — и сверяется строка, которую увидит
бухгалтер: в полях только поля, спор значений — «несколько вариантов», строка —
«Проверить», в заявке очереди текста гостя нет.
"""

from __future__ import annotations

import time

import pytest

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.api import RequisiteField, RequisitesStatus, UploadInput
from hospitality.modules.documents.extraction import normalize_field, parse_requisites
from hospitality.modules.documents.tests.attacks import (
    ATTACKS,
    BIN,
    IIK,
    Attack,
    AttackKind,
    assert_only_fields,
)
from hospitality.modules.documents.tests.conftest import Hotels, submit_form
from hospitality.modules.requests import api as requests_api
from hospitality.platform.file_text import MAX_CHARS
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context

from .test_documents import WEEK_AGO


def test_the_set_covers_every_attack_class_of_the_issue() -> None:
    assert {attack.kind for attack in ATTACKS} == set(AttackKind)
    assert len({attack.name for attack in ATTACKS}) == len(ATTACKS)


@pytest.mark.parametrize("attack", ATTACKS, ids=lambda attack: attack.name)
async def test_attack_leaves_only_fields_for_the_human_to_check(
    hotels: Hotels, attack: Attack
) -> None:
    uploads = [UploadInput(filename=name, data=build()) for name, build in attack.files]
    with tenant_context(hotels.a.tenant_id):
        read = await documents_api.submit_act_request(
            submit_form(hotels.a, bill_to=attack.bill_to, note=attack.note), uploads
        )
        await documents_api.extract_requisites(read.id)
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - WEEK_AGO, created_before=utc_now() + WEEK_AGO
        )
        request = await requests_api.get_request(read.request_id)

    assert_only_fields(row.requisites)
    assert row.requisites == attack.expected
    assert {
        field
        for field, warning in row.warnings.items()
        if warning is documents_api.FieldWarning.AMBIGUOUS
    } == attack.ambiguous
    assert row.extraction_state is documents_api.ExtractionState.DONE
    assert row.status is RequisitesStatus.TO_CHECK
    # Заявка в очереди и уведомлениях служб: ни слова гостя, ни текста файла.
    assert request.summary == documents_api.REQUEST_SUMMARY
    assert request.details == f"Файлов: {len(uploads)}"


@pytest.mark.parametrize(
    "flood",
    [
        "KZ00 " * 20_000,
        "ТОО «" + "а" * 99_990,
        "БИН " * 25_000,
        "Банк\n" * 20_000,
        "Адрес" + ":" * 99_990,
        "1" * 100_000,
    ],
    ids=["iik_starts", "open_quote", "bin_labels", "bank_lines", "colons", "digits"],
)
def test_text_at_the_ceiling_is_parsed_in_linear_time(flood: str) -> None:
    """Пять файлов по потолку текста: разбор идёт в процессе воркера, не в песочнице."""
    started = time.monotonic()

    parsed = parse_requisites("", [flood[:MAX_CHARS]] * documents_api.MAX_FILES)

    # Замер аудита — меньше секунды; запас на загруженный CI в десятки раз.
    assert time.monotonic() - started < 30
    assert_only_fields(parsed.values)


@pytest.mark.parametrize("zero", [0x0660, 0x06F0, 0x0966, 0xFF10, 0x1D7CE])
def test_digits_of_another_script_are_not_a_code(zero: int) -> None:
    """«12 цифр» — ASCII: правка бухгалтера и ответ модели идут через ту же проверку."""
    foreign_bin = "".join(chr(zero + int(char)) for char in BIN)
    foreign_iik = "KZ" + "".join(chr(zero + int(char)) for char in IIK[2:4]) + IIK[4:]

    assert normalize_field(RequisiteField.BIN_IIN, foreign_bin) is None
    assert normalize_field(RequisiteField.IIK, foreign_iik) is None
    assert parse_requisites("", [f"БИН: {foreign_bin}\nИИК: {foreign_iik}"]).values == (
        documents_api.Requisites()
    )


def test_value_of_a_field_is_one_line_of_printable_characters() -> None:
    """Правка бухгалтера и ответ модели: невидимое убрано, пробелы любой ширины — пробел."""
    assert (
        normalize_field(RequisiteField.COMPANY_NAME, " ТОО «Ромашка‮»\r\n\x00 Строй\t")
        == "ТОО «Ромашка» Строй"
    )
    assert normalize_field(RequisiteField.IIK, "KZ86 125K ZT50 0410​ 0100") == IIK
    assert normalize_field(RequisiteField.BIN_IIN, "​ ‮") == ""
