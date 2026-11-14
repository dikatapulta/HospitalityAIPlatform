"""Контракт `AccountingPort.render_act` — черновик АВР по форме Р-1 (R-7, ADR-007, spec 0086).

Канон — `tests/test_accounting_port.py`: один набор на ВСЕ адаптеры, у каждого
свой «читатель» файла. Контракт: номер, дата, обе стороны, строки по порядку и
суммы — ровно из снимка; имя «АВР № … от …» с расширением; тот же снимок — те
же байты. Ниже — особенности Excel (числа числами, текст текстом, формул нет) и
PDF (текст извлекается, кириллица и казахские буквы целы, A4 альбомом, шрифт встроен).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree

import pytest
from pypdf import PdfReader

from hospitality.integrations.accounting import FakeAccountingAdapter, OneCXlsxAccountingAdapter
from hospitality.integrations.accounting.act_form import COLUMN_HEADERS, FOOTNOTES, TITLE
from hospitality.integrations.accounting.act_pdf import PDF_CONTENT_TYPE
from hospitality.integrations.accounting.xlsx import XLSX_CONTENT_TYPE
from hospitality.shared.ports.accounting import (
    AccountingPort,
    ActDocument,
    ActFileFormat,
    ActLine,
    ActParty,
)

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# Число в PDF — текстом по-русски: «45 000,00», «1,5» (пробел у pypdf — обычный).
_PDF_NUMBER = re.compile(r"\d{1,3}(?: \d{3})*(?:,\d+)?")


@dataclass(frozen=True)
class Seen:
    """Что читатель нашёл в файле: все тексты и все числа подряд."""

    texts: list[str]
    numbers: list[Decimal]


def read_cells(content: bytes) -> dict[str, str | Decimal]:
    """Ячейки первого листа по адресу: текст — из общей таблицы строк, число — числом."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared = [
            "".join(text.text or "" for text in item.iter(f"{_MAIN}t"))
            for item in ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        ]
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cells: dict[str, str | Decimal] = {}
    for cell in sheet.iter(f"{_MAIN}c"):
        raw = cell.findtext(f"{_MAIN}v", "")
        cells[cell.get("r", "")] = shared[int(raw)] if cell.get("t") == "s" else Decimal(raw)
    return cells


def _xlsx_seen(content: bytes) -> Seen:
    values = list(read_cells(content).values())
    return Seen(
        texts=[value for value in values if isinstance(value, str)],
        numbers=[value for value in values if isinstance(value, Decimal)],
    )


def pdf_fragments(content: bytes) -> list[str]:
    """Тексты PDF по одному на вывод текста — ячейку или строку абзаца, все страницы."""
    fragments: list[str] = []

    def visit(text: str, *_: object) -> None:
        if text.strip():
            fragments.append(text.strip())

    for page in PdfReader(io.BytesIO(content)).pages:
        page.extract_text(visitor_text=visit)
    return fragments


def _pdf_seen(content: bytes) -> Seen:
    texts = pdf_fragments(content)
    numbers = [
        Decimal(text.replace(" ", "").replace(",", "."))
        for text in texts
        if _PDF_NUMBER.fullmatch(text)
    ]
    return Seen(texts, numbers)


def _json_seen(content: bytes) -> Seen:
    payload = json.loads(content)
    texts = [payload["number"], payload["contract"]]
    for party in (payload["customer"], payload["executor"]):
        texts += [party["name"], party["bin_iin"], party["address"], party["contact"]]
    numbers = [Decimal(payload["total"])]
    for line in payload["lines"]:
        texts += [line["service"], line["service_date"], line["unit"]]
        numbers += [Decimal(line[key]) for key in ("quantity", "price", "cost")]
    return Seen(texts, numbers)


@dataclass(frozen=True)
class Case:
    make: Callable[[], AccountingPort]
    seen: Callable[[bytes], Seen]
    extension: str
    file_format: ActFileFormat


CASES = {
    "onec_xlsx": Case(OneCXlsxAccountingAdapter, _xlsx_seen, ".xlsx", ActFileFormat.XLSX),
    "onec_pdf": Case(OneCXlsxAccountingAdapter, _pdf_seen, ".pdf", ActFileFormat.PDF),
    "fake": Case(FakeAccountingAdapter, _json_seen, ".json", ActFileFormat.PDF),
}


@pytest.fixture(params=list(CASES))
def case(request: pytest.FixtureRequest) -> Case:
    return CASES[request.param]


def act(*, number: str = "15", service: str = "Проживание, номер 512") -> ActDocument:
    return ActDocument(
        number=number,
        composed_on=date(2026, 11, 11),
        contract="№ 12 от 01.11.2026",
        customer=ActParty(name="ТОО «Арман»", bin_iin="040140000385", address="Алматы, Абая 10"),
        executor=ActParty(
            name="ТОО «Отель Тест»",
            bin_iin="123456789012",
            address="Алматы, Абая 1",
            contact="+7 727 000 00 00",
        ),
        lines=(
            ActLine(
                service=service,
                service_date="10.11–12.11.2026",
                unit="сутки",
                quantity=Decimal("2"),
                price=Decimal("22500.00"),
                cost=Decimal("45000.00"),
            ),
            ActLine(
                service="Завтрак",
                service_date="",
                unit="услуга",
                quantity=Decimal("1.5"),
                price=Decimal("3000.00"),
                cost=Decimal("4500.00"),
            ),
        ),
        quantity_total=None,
        total=Decimal("49500.00"),
    )


async def test_file_carries_number_parties_lines_and_sums(case: Case) -> None:
    rendered = await case.make().render_act(act(), file_format=case.file_format)

    seen = case.seen(rendered.content)
    joined = " | ".join(seen.texts)
    for value in (
        "15",
        "№ 12 от 01.11.2026",
        "ТОО «Арман»",
        "040140000385",
        "Алматы, Абая 10",
        "ТОО «Отель Тест»",
        "123456789012",
        "+7 727 000 00 00",
        "Проживание, номер 512",
        "10.11–12.11.2026",
        "Завтрак",
    ):
        assert value in joined, value
    for number in ("2", "22500", "45000", "1.5", "3000", "4500", "49500"):
        assert Decimal(number) in seen.numbers, number


async def test_filename_and_reproducible_bytes(case: Case) -> None:
    adapter = case.make()

    first = await adapter.render_act(act(number="15/2026"), file_format=case.file_format)
    second = await adapter.render_act(act(number="15/2026"), file_format=case.file_format)

    assert first.filename == f"АВР № 15-2026 от 11.11.2026{case.extension}"
    assert first.content == second.content


# --- Excel: форма Р-1 ------------------------------------------------------------


async def test_xlsx_lays_out_form_r1() -> None:
    rendered = await OneCXlsxAccountingAdapter().render_act(act(), file_format=ActFileFormat.XLSX)

    cells = read_cells(rendered.content)
    texts = [value for value in cells.values() if isinstance(value, str)]
    assert rendered.content_type == XLSX_CONTENT_TYPE
    assert TITLE in texts and "Форма Р-1" in texts and "Итого" in texts
    assert all(header in texts for header in COLUMN_HEADERS)
    assert all(footnote in texts for footnote in FOOTNOTES)
    assert [cells[f"{column}13"] for column in "ABCDEFGH"] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
    ]
    # Первая строка таблицы: № по порядку, услуга, дата, пустая графа 4, единица, числа.
    assert [cells[f"{column}14"] for column in "ABCDEFGH"] == [
        "1",
        "Проживание, номер 512",
        "10.11–12.11.2026",
        "",
        "сутки",
        Decimal("2"),
        Decimal("22500"),
        Decimal("45000"),
    ]
    # «Итого»: разные единицы — количества нет, в цене «х», сумма — числом.
    assert (cells["F16"], cells["G16"], cells["H16"]) == ("", "х", Decimal("49500"))


async def test_xlsx_keeps_formula_like_text_as_text() -> None:
    rendered = await OneCXlsxAccountingAdapter().render_act(
        act(service='=HYPERLINK("http://evil","x")'), file_format=ActFileFormat.XLSX
    )

    with zipfile.ZipFile(io.BytesIO(rendered.content)) as archive:
        for name in archive.namelist():
            ElementTree.fromstring(archive.read(name))
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    assert not list(sheet.iter(f"{_MAIN}f"))
    assert read_cells(rendered.content)["B14"] == '=HYPERLINK("http://evil","x")'


async def test_fake_remembers_every_act() -> None:
    fake = FakeAccountingAdapter()

    await fake.render_act(act(number="1"), file_format=ActFileFormat.XLSX)
    await fake.render_act(act(number="2"), file_format=ActFileFormat.XLSX)

    assert [document.number for document in fake.acts] == ["1", "2"]


# --- PDF: форма Р-1 ---------------------------------------------------------------

# A4 альбомом в пунктах PDF: 297 × 210 мм.
_A4_LANDSCAPE = (Decimal("841.89"), Decimal("595.28"))


def _joined(fragments: list[str]) -> str:
    """Тексты одной строкой: перенос внутри ячейки или абзаца — пробел."""
    return " ".join(fragments)


async def test_pdf_lays_out_form_r1() -> None:
    rendered = await OneCXlsxAccountingAdapter().render_act(act(), file_format=ActFileFormat.PDF)

    reader = PdfReader(io.BytesIO(rendered.content))
    fragments = pdf_fragments(rendered.content)
    joined = _joined(fragments)
    box = reader.pages[0].mediabox
    assert rendered.content_type == PDF_CONTENT_TYPE == "application/pdf"
    assert rendered.content.startswith(b"%PDF-")
    assert len(reader.pages) == 1
    assert (round(Decimal(str(box.width)), 2), round(Decimal(str(box.height)), 2)) == _A4_LANDSCAPE
    assert TITLE in fragments and "Форма Р-1" in fragments
    assert all(header in joined for header in COLUMN_HEADERS)
    assert all(footnote in joined for footnote in FOOTNOTES)
    assert "Заказчик: ТОО «Арман», Алматы, Абая 10" in fragments
    # Строка таблицы: № по порядку, услуга, дата, единица, количество, цена, стоимость.
    # Неразрывный пробел между тысячами шрифт рисует знаком пробела — pypdf отдаёт обычный.
    first = fragments.index("Проживание, номер 512")
    assert fragments[first - 1 : first + 6] == [
        "1",
        "Проживание, номер 512",
        "10.11–12.11.2026",
        "сутки",
        "2",
        "22 500,00",
        "45 000,00",
    ]
    # «Итого»: разные единицы — количества нет, в цене «х», сумма.
    total = fragments.index("Итого")
    assert fragments[total + 1 : total + 3] == ["х", "49 500,00"]


async def test_pdf_keeps_kazakh_letters_as_text_in_embedded_font() -> None:
    kazakh = act().model_copy(
        update={
            "customer": ActParty(
                name="ЖШС «Әйгерім Құрылыс Өңірі» — ғимарат, һауа, ұя, үкі, іс",
                bin_iin="040140000385",
                address="Алматы қ., Әл-Фараби даңғылы 77/8",
            )
        }
    )

    rendered = await OneCXlsxAccountingAdapter().render_act(kazakh, file_format=ActFileFormat.PDF)

    joined = _joined(pdf_fragments(rendered.content))
    assert "ЖШС «Әйгерім Құрылыс Өңірі» — ғимарат, һауа, ұя, үкі, іс" in joined
    assert "Алматы қ., Әл-Фараби даңғылы 77/8" in joined
    # Шрифт встроен (TrueType — /FontFile2), а не взят у программы просмотра.
    assert b"/FontFile2" in rendered.content and b"NotoSans" in rendered.content
    assert b"/Helvetica" not in rendered.content


async def test_pdf_long_act_breaks_pages_and_keeps_signatures_with_footnotes() -> None:
    line = ActLine(
        service="Проживание — " + "длинное наименование услуги " * 7,
        service_date="10.11.2026",
        unit="услуга",
        quantity=Decimal("99999.125"),
        price=Decimal("9999999999.99"),
        cost=Decimal("1234567.89"),
    )
    long_act = act().model_copy(update={"lines": (line,) * 20})

    rendered = await OneCXlsxAccountingAdapter().render_act(long_act, file_format=ActFileFormat.PDF)

    pages = PdfReader(io.BytesIO(rendered.content)).pages
    last = _joined([text.strip() for text in pages[-1].extract_text().splitlines()])
    assert len(pages) > 1
    assert pdf_fragments(rendered.content).count("99 999,125") == 20
    assert "Сдал (Исполнитель)" in last and "Принял (Заказчик)" in last
    assert FOOTNOTES[2] in last
