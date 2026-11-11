"""Минимальная книга Excel (.xlsx) из строк ячеек — без сторонних библиотек.

Один лист. Текст — через общую таблицу строк (`sharedStrings`), как пишет сам
Excel: так файл читают и Excel, и загрузка 1С, а значение «=…» из реквизитов
гостя остаётся строкой, а не формулой. Формат текстовых ячеек «@»: бухгалтер
правит БИН/ИИН в Excel — ведущий ноль не пропадает. Число (`Decimal`) —
числом: сумму акта бухгалтер может сложить (spec 0086 Р-7). Формул нет вовсе.
Метки времени в архиве постоянные: одна таблица — один файл.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from xml.sax.saxutils import escape

XLSX_CONTENT_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_MAIN_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_HEADER: Final = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# Формат ячеек: 49 — встроенный «@» (текст), 0 — «Общий», 164 — свой денежный.
_TEXT_FORMAT: Final = 49
_NUMBER_FORMAT: Final = 0
_MONEY_FORMAT: Final = 164

_SPREADSHEET_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_PART_TYPES: Final = {
    "/xl/workbook.xml": "sheet.main+xml",
    "/xl/worksheets/sheet1.xml": "worksheet+xml",
    "/xl/styles.xml": "styles+xml",
    "/xl/sharedStrings.xml": "sharedStrings+xml",
}
_CONTENT_TYPES: Final = (
    _HEADER + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    + "".join(
        f'<Override PartName="{part}" ContentType="{_SPREADSHEET_TYPE}.{kind}"/>'
        for part, kind in _PART_TYPES.items()
    )
    + "</Types>"
)
_ROOT_RELS: Final = (
    _HEADER + f'<Relationships xmlns="{_PKG_REL_NS}">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK_RELS: Final = (
    _HEADER + f'<Relationships xmlns="{_PKG_REL_NS}">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_NS}/styles" Target="styles.xml"/>'
    f'<Relationship Id="rId3" Type="{_REL_NS}/sharedStrings" Target="sharedStrings.xml"/>'
    "</Relationships>"
)


@dataclass(frozen=True)
class CellStyle:
    """Вид ячейки. `money` — у числа: «1 234,50» в русском Excel."""

    bold: bool = False
    border: bool = False
    center: bool = False
    wrap: bool = False
    money: bool = False


@dataclass(frozen=True)
class Cell:
    """Ячейка листа: `str` — текст, `Decimal` — число."""

    value: str | Decimal
    style: CellStyle = CellStyle()


def clean_cell_text(value: str) -> str:
    """Значение без символов, которых не вынесет XML (управляющие, одиночные суррогаты)."""
    return "".join(char for char in value if _is_xml_char(ord(char)))


def _is_xml_char(code: int) -> bool:
    # XML 1.0 §2.2: с другим символом файл не откроется вовсе.
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def build_xlsx(sheet_name: str, rows: Sequence[Sequence[str]], *, widths: Sequence[int]) -> bytes:
    """Таблица из одного листа: `rows[0]` — заголовок жирным, все ячейки — текст."""
    header, plain = CellStyle(bold=True), CellStyle()
    return build_sheet(
        sheet_name,
        [
            [Cell(value, header if number == 0 else plain) for value in row]
            for number, row in enumerate(rows)
        ],
        widths=widths,
    )


def build_sheet(
    sheet_name: str,
    rows: Sequence[Sequence[Cell | None]],
    *,
    widths: Sequence[int],
    merges: Sequence[str] = (),
    heights: Mapping[int, int] | None = None,
    landscape: bool = False,
) -> bytes:
    """Книга из одного листа ячеек. None — пустая ячейка.

    `widths` — ширина столбцов в знаках, `merges` — объединения вида «A11:A12»,
    `heights` — высота строки в пунктах по её номеру с единицы, `landscape` —
    печать на A4 альбомом, вписанная в ширину листа (форма шире книжного A4).
    """
    strings: dict[str, int] = {}
    styles: dict[tuple[int, CellStyle], int] = {}
    sheet_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column, cell in enumerate(row):
            if cell is None:
                continue
            ref = f"{_column_letters(column)}{row_number}"
            if isinstance(cell.value, Decimal):
                number_format = _MONEY_FORMAT if cell.style.money else _NUMBER_FORMAT
                style = styles.setdefault((number_format, cell.style), len(styles) + 1)
                cells.append(f'<c r="{ref}" s="{style}"><v>{_number(cell.value)}</v></c>')
            else:
                style = styles.setdefault((_TEXT_FORMAT, cell.style), len(styles) + 1)
                index = strings.setdefault(clean_cell_text(cell.value), len(strings))
                cells.append(f'<c r="{ref}" s="{style}" t="s"><v>{index}</v></c>')
        height = (heights or {}).get(row_number)
        height_attrs = f' ht="{height}" customHeight="1"' if height else ""
        sheet_rows.append(f'<row r="{row_number}"{height_attrs}>{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{n}" max="{n}" width="{width}" customWidth="1"/>'
        for n, width in enumerate(widths, start=1)
    )
    merged = (
        f'<mergeCells count="{len(merges)}">'
        + "".join(f'<mergeCell ref="{ref}"/>' for ref in merges)
        + "</mergeCells>"
        if merges
        else ""
    )
    # Порядок частей листа задан схемой (CT_Worksheet): sheetPr — первым, pageSetup —
    # после mergeCells и полей страницы.
    fit = '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>' if landscape else ""
    page = (
        '<pageMargins left="0.5" right="0.5" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
        '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        if landscape
        else ""
    )
    sheet = (
        _HEADER + f'<worksheet xmlns="{_MAIN_NS}">{fit}<cols>{cols}</cols>'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>{merged}{page}</worksheet>"
    )
    text_cells = sum(
        1 for row in rows for cell in row if cell is not None and isinstance(cell.value, str)
    )
    shared = (
        _HEADER
        + f'<sst xmlns="{_MAIN_NS}" count="{text_cells}" uniqueCount="{len(strings)}">'
        + "".join(f'<si><t xml:space="preserve">{escape(text)}</t></si>' for text in strings)
        + "</sst>"
    )
    workbook = (
        _HEADER + f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets>'
        f'<sheet name="{escape(sheet_name, {chr(34): "&quot;"})}" sheetId="1" r:id="rId1"/>'
        "</sheets></workbook>"
    )
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS,
        "xl/styles.xml": _styles(list(styles)),
        "xl/sharedStrings.xml": shared,
        "xl/worksheets/sheet1.xml": sheet,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    return buffer.getvalue()


def _styles(used: Sequence[tuple[int, CellStyle]]) -> str:
    """Таблица стилей: xf 0 — обычный, дальше — по одному на вид ячейки в порядке появления."""
    xfs = ['<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>']
    for number_format, style in used:
        alignment = (
            "<alignment"
            + (' horizontal="center" vertical="center"' if style.center else ' vertical="top"')
            + (' wrapText="1"' if style.wrap else "")
            + "/>"
        )
        xfs.append(
            f'<xf numFmtId="{number_format}" fontId="{int(style.bold)}" fillId="0" '
            f'borderId="{int(style.border)}" xfId="0" applyNumberFormat="1" applyFont="1" '
            f'applyBorder="1" applyAlignment="1">{alignment}</xf>'
        )
    thin = "".join(
        f'<{side} style="thin"><color auto="1"/></{side}>'
        for side in ("left", "right", "top", "bottom")
    )
    return "".join(
        (
            _HEADER,
            f'<styleSheet xmlns="{_MAIN_NS}">',
            f'<numFmts count="1"><numFmt numFmtId="{_MONEY_FORMAT}" formatCode="#,##0.00"/>',
            "</numFmts>",
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>',
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>',
            '<fills count="2"><fill><patternFill patternType="none"/></fill>',
            '<fill><patternFill patternType="gray125"/></fill></fills>',
            '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>',
            f"<border>{thin}<diagonal/></border></borders>",
            '<cellStyleXfs count="1">',
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>',
            f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>',
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>',
            "</styleSheet>",
        )
    )


def _number(value: Decimal) -> str:
    """Десятичная запись без экспоненты: «1E+3» Excel прочтёт, но 1С — не обязана."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _column_letters(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters
