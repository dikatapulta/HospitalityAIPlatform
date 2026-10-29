"""Минимальная книга Excel (.xlsx) из таблицы строк — без сторонних библиотек.

Один лист, все ячейки — текст через общую таблицу строк (`sharedStrings`), как
пишет сам Excel: так файл читают и Excel, и загрузка 1С, а значение «=…» из
реквизитов гостя остаётся строкой, а не формулой. Формат ячеек «@» (текст):
бухгалтер правит БИН/ИИН в Excel — ведущий ноль не пропадает. Первая строка —
заголовок жирным. Метки времени в архиве постоянные: одна таблица — один файл.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from typing import Final
from xml.sax.saxutils import escape

XLSX_CONTENT_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_MAIN_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_HEADER: Final = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
# Стили: 0 — обычный, 1 — текст «@», 2 — текст «@» жирным (заголовок).
_TEXT_STYLE: Final = 1
_HEADER_STYLE: Final = 2

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
_STYLES: Final = (
    _HEADER + f'<styleSheet xmlns="{_MAIN_NS}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="49" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" '
    'applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


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
    """Книга из одного листа: `rows[0]` — заголовок, `widths` — ширина столбцов в знаках."""
    strings: dict[str, int] = {}
    sheet_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        style = _HEADER_STYLE if row_number == 1 else _TEXT_STYLE
        cells = []
        for column, value in enumerate(row):
            index = strings.setdefault(clean_cell_text(value), len(strings))
            ref = f"{_column_letters(column)}{row_number}"
            cells.append(f'<c r="{ref}" s="{style}" t="s"><v>{index}</v></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{n}" max="{n}" width="{width}" customWidth="1"/>'
        for n, width in enumerate(widths, start=1)
    )
    sheet = (
        _HEADER + f'<worksheet xmlns="{_MAIN_NS}"><cols>{cols}</cols>'
        f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    shared = (
        _HEADER + f'<sst xmlns="{_MAIN_NS}" count="{sum(len(row) for row in rows)}" '
        f'uniqueCount="{len(strings)}">'
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
        "xl/styles.xml": _STYLES,
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


def _column_letters(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters
