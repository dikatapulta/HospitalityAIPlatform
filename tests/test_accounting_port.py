"""Контрактные тесты порта бухгалтерии (R-7, ADR-007, spec 0052).

Канон — `tests/test_file_storage_port.py`: один набор на ВСЕ адаптеры порта.
Каждый адаптер приходит со своим «читателем» файла: Excel разбирается как
настоящий .xlsx (zip + XML общей таблицы строк), Fake — как JSON. Ниже
контракта — особенности Excel для 1С: текст, а не формула, заголовок и даты.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree

import pytest

from hospitality.integrations.accounting import (
    FakeAccountingAdapter,
    OneCXlsxAccountingAdapter,
    build_accounting,
)
from hospitality.integrations.accounting.onec_xlsx import HEADER
from hospitality.integrations.accounting.xlsx import XLSX_CONTENT_TYPE, build_xlsx
from hospitality.shared.ports.accounting import AccountingPort, CounterpartyRow, ExportFile

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PERIOD = {"period_from": date(2026, 10, 1), "period_to": date(2026, 10, 29)}


def read_xlsx(content: bytes) -> list[list[str]]:
    """Все строки первого листа текстом (ячейки — индексы общей таблицы строк)."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared = [
            "".join(text.text or "" for text in item.iter(f"{_MAIN}t"))
            for item in ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        ]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    return [
        [shared[int(cell.findtext(f"{_MAIN}v", "-1"))] for cell in row.iter(f"{_MAIN}c")]
        for row in sheet.iter(f"{_MAIN}row")
    ]


def _xlsx_rows(content: bytes) -> list[list[str]]:
    table = read_xlsx(content)
    assert table[0] == list(HEADER)
    return [row[:7] for row in table[1:]]


def _json_rows(content: bytes) -> list[list[str]]:
    table = json.loads(content)
    assert table["header"][:7] == [
        "company_name",
        "bin_iin",
        "address",
        "iik",
        "bik",
        "bank_name",
        "room_number",
    ]
    return [row[:7] for row in table["rows"]]


@dataclass(frozen=True)
class Case:
    make: Callable[[], AccountingPort]
    rows: Callable[[bytes], list[list[str]]]  # строки данных: реквизиты и номер
    extension: str


CASES = {
    "onec_xlsx": Case(OneCXlsxAccountingAdapter, _xlsx_rows, ".xlsx"),
    "fake": Case(FakeAccountingAdapter, _json_rows, ".json"),
}


@pytest.fixture(params=list(CASES))
def case(request: pytest.FixtureRequest) -> Case:
    return CASES[request.param]


def counterparty(
    name: str = "ТОО «Арман»", bin_iin: str = "940140000385", **extra: str
) -> CounterpartyRow:
    values = {
        "company_name": name,
        "bin_iin": bin_iin,
        "address": "Алматы, пр. Абая 10",
        "iik": "KZ86125KZT5004100100",
        "bik": "HSBKKZKX",
        "bank_name": "АО «Народный Банк Казахстана»",
        "room_number": "512",
        "check_in": "2026-10-20",
        "check_out": "2026-10-23",
    }
    values.update(extra)
    return CounterpartyRow.model_validate(values)


def _texts(row: CounterpartyRow) -> list[str]:
    return [
        row.company_name,
        row.bin_iin,
        row.address,
        row.iik,
        row.bik,
        row.bank_name,
        row.room_number,
    ]


async def test_rows_in_given_order_and_nothing_more(case: Case) -> None:
    rows = [counterparty("ТОО Б", "971240001315"), counterparty("ИП Абай", "", iik="")]

    exported = await case.make().export_counterparties(rows, **PERIOD)

    assert case.rows(exported.content) == [_texts(row) for row in rows]


async def test_empty_list_is_header_only(case: Case) -> None:
    exported = await case.make().export_counterparties([], **PERIOD)

    assert case.rows(exported.content) == []


async def test_values_are_text_as_given(case: Case) -> None:
    """Формула из реквизитов гостя — строка, ведущий ноль БИН цел, пробелы на месте."""
    row = counterparty('=HYPERLINK("http://evil","x")', "040140000385", address="  Абая 10  ")

    exported = await case.make().export_counterparties([row], **PERIOD)

    assert case.rows(exported.content) == [_texts(row)]


async def test_control_characters_are_dropped(case: Case) -> None:
    exported = await case.make().export_counterparties(
        [counterparty("ТОО\x01 Арман\x00", bank_name="Банк\ufffe")], **PERIOD
    )

    (row,) = case.rows(exported.content)
    assert row[0] == "ТОО Арман" and row[5] == "Банк"


async def test_filename_has_period_and_extension(case: Case) -> None:
    exported = await case.make().export_counterparties([counterparty()], **PERIOD)

    assert exported.filename.endswith(case.extension)
    assert "01.10.2026" in exported.filename or "2026-10-01" in exported.filename
    assert "29.10.2026" in exported.filename or "2026-10-29" in exported.filename


async def test_check_is_ready(case: Case) -> None:
    assert await case.make().check() is True


# --- Excel для 1С ---------------------------------------------------------------


async def test_xlsx_has_no_formulas_and_every_cell_is_text() -> None:
    exported = await OneCXlsxAccountingAdapter().export_counterparties(
        [counterparty("=1+1"), counterparty("@SUM(A1)")], **PERIOD
    )

    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cells = list(sheet.iter(f"{_MAIN}c"))
    assert cells and all(cell.get("t") == "s" for cell in cells)
    assert not list(sheet.iter(f"{_MAIN}f"))


async def test_xlsx_header_dates_type_and_name() -> None:
    exported = await OneCXlsxAccountingAdapter().export_counterparties([counterparty()], **PERIOD)

    table = read_xlsx(exported.content)
    assert table[0] == [
        "Контрагент",
        "БИН/ИИН",
        "Адрес",
        "ИИК",
        "БИК",
        "Банк",
        "Номер",
        "Заезд",
        "Выезд",
    ]
    assert table[1][7:] == ["20.10.2026", "23.10.2026"]
    assert exported.content_type == XLSX_CONTENT_TYPE
    assert exported.filename == "Контрагенты для 1С 01.10.2026–29.10.2026.xlsx"


def test_xlsx_is_a_valid_package_and_reproducible() -> None:
    """Каждая часть — XML; одна таблица — одни байты; столбцы после Z — AA, AB."""
    table = [[f"h{n}" for n in range(28)], [str(n) for n in range(28)]]

    content = build_xlsx("Лист", table, widths=[10] * 28)

    assert content == build_xlsx("Лист", table, widths=[10] * 28)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            ElementTree.fromstring(archive.read(name))
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
    assert 'r="AB2"' in sheet
    assert read_xlsx(content) == table


async def test_fake_keeps_one_card_per_bin_and_every_file() -> None:
    fake = FakeAccountingAdapter()

    await fake.export_counterparties([counterparty("ТОО Арман")], **PERIOD)
    second: ExportFile = await fake.export_counterparties(
        [counterparty("ТОО Арман Плюс")], **PERIOD
    )

    assert list(fake.counterparties) == ["940140000385"]
    assert fake.counterparties["940140000385"].company_name == "ТОО Арман Плюс"
    assert fake.exports[-1] == second and len(fake.exports) == 2


def test_composition_root_gets_the_excel_adapter() -> None:
    assert build_accounting().backend == "onec_xlsx"
