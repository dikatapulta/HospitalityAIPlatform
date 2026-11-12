"""Черновик АВР по форме Р-1 файлом PDF (spec 0086 Р-1, Р-7; PR 3 серии).

Раскладка — та же, что у Excel (`act_xlsx.py`), на A4 альбомом: стороны с
ИИН/БИН, договор, номер и дата, таблица из восьми граф с «Итого», запасы,
приложение, «Сдал»/«Принял», М.П., дата подписания и три сноски. Надписи формы —
`act_form.py`. Подпись, печать и дату подписания ставят люди на бумаге — здесь
только линии под них.

Собирает `fpdf2` со встроенным шрифтом Noto Sans (`fonts/`, лицензия OFL —
`fonts/NotoSans-OFL.txt`): в нём кириллица и казахские буквы, «₸» и «№».
Значения — ровно из снимка `ActDocument`, адаптер ничего не считает; числа —
текстом по-русски: «45 000,00». Дата создания файла — дата составления акта,
поэтому тот же снимок даёт те же байты.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Final

from fpdf import FPDF
from fpdf.enums import Align, MethodReturnValue, XPos, YPos
from fpdf.fonts import FontFace

from hospitality.integrations.accounting import act_form as form
from hospitality.integrations.accounting.act_form import act_filename
from hospitality.shared.ports.accounting import ActDocument, ActParty, ExportFile

PDF_CONTENT_TYPE: Final = "application/pdf"
FONT_DIR: Final = Path(__file__).with_name("fonts")
_FAMILY: Final = "NotoSans"
# A4 альбомом — 297 мм, поля по 10 мм.
_MARGIN: Final = 10.0
_WIDTH: Final = 277.0
# Ширина граф 1–8 в мм — пропорции Excel: наименование и сведения об отчёте длинные.
_COLUMNS: Final = (14.0, 88.0, 30.0, 42.0, 20.0, 22.0, 29.0, 32.0)
_BIN_LABEL: Final = 30.0
_BIN_VALUE: Final = 40.0
_NUMBER_BOX: Final = 45.0
_BODY: Final = 9.0
_SMALL: Final = 7.0
_TITLE_SIZE: Final = 11.0
_LINE: Final = 4.5
_SMALL_LINE: Final = 3.5
# Подписи, М.П. и сноски — одним блоком: разрыв страницы между ними нечитаем.
_TAIL_HEIGHT: Final = 48.0
_NBSP: Final = " "


def render_act_pdf(act: ActDocument) -> ExportFile:
    """PDF формы Р-1 из снимка: тот же снимок — те же байты."""
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_margins(_MARGIN, _MARGIN, _MARGIN)
    pdf.set_auto_page_break(auto=True, margin=_MARGIN)
    pdf.add_font(_FAMILY, "", FONT_DIR / "NotoSans-Regular.ttf")
    pdf.add_font(_FAMILY, "B", FONT_DIR / "NotoSans-Bold.ttf")
    pdf.set_creation_date(datetime.combine(act.composed_on, time(), tzinfo=UTC))
    pdf.add_page()

    _form_source(pdf)
    _party(pdf, "Заказчик", act.customer)
    _party(pdf, "Исполнитель", act.executor)
    _paragraph(pdf, form.contract_line(act))
    pdf.ln(2)
    _title_and_number(pdf, act)
    pdf.ln(3)
    _table(pdf, act)
    pdf.ln(3)
    _paragraph(pdf, form.SUPPLIES)
    _paragraph(pdf, form.ATTACHMENT)
    _signatures(pdf)
    return ExportFile(
        filename=act_filename(act, "pdf"),
        content_type=PDF_CONTENT_TYPE,
        content=bytes(pdf.output()),
    )


def _form_source(pdf: FPDF) -> None:
    """Справа вверху: «Приложение 50 к приказу …» и «Форма Р-1»."""
    pdf.set_font(_FAMILY, size=_SMALL)
    for text in (form.FORM_SOURCE, form.FORM_NAME):
        pdf.cell(_WIDTH, _SMALL_LINE, text, align=Align.R, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)


def _party(pdf: FPDF, label: str, party: ActParty) -> None:
    """«Заказчик: наименование, адрес, связь» слева, «ИИН/БИН» и значение — в рамках справа."""
    pdf.set_font(_FAMILY, size=_BODY)
    text = form.party_line(label, party)
    width = _WIDTH - _BIN_LABEL - _BIN_VALUE
    height = max(_line_count(pdf, width, text), 2) * _LINE
    top = pdf.get_y()
    pdf.multi_cell(width, _LINE, text, align=Align.L, new_x=XPos.RIGHT, new_y=YPos.TOP)
    pdf.set_xy(_MARGIN + width, top)
    pdf.cell(_BIN_LABEL, height, form.BIN_IIN, border=1, align=Align.C)
    pdf.cell(_BIN_VALUE, height, party.bin_iin, border=1, align=Align.C)
    pdf.set_xy(_MARGIN, top + height + 1.5)


def _title_and_number(pdf: FPDF, act: ActDocument) -> None:
    """Название акта по центру слева, «Номер документа» и «Дата составления» — рамками справа."""
    boxes = _MARGIN + _WIDTH - 2 * _NUMBER_BOX
    pdf.set_font(_FAMILY, size=_SMALL)
    pdf.set_x(boxes)
    pdf.cell(_NUMBER_BOX, 6, form.DOCUMENT_NUMBER, border=1, align=Align.C)
    pdf.cell(
        _NUMBER_BOX,
        6,
        form.COMPOSED_ON,
        border=1,
        align=Align.C,
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_font(_FAMILY, size=_BODY)
    # Номер — до 30 знаков (spec 0086 Р-3): длинный переносится внутри своей рамки.
    lines = _line_count(pdf, _NUMBER_BOX, act.number)
    height = lines * _LINE + 1.5
    top = pdf.get_y()
    pdf.set_font(_FAMILY, "B", _TITLE_SIZE)
    pdf.cell(boxes - _MARGIN, height, form.TITLE, align=Align.C)
    pdf.set_font(_FAMILY, size=_BODY)
    pdf.multi_cell(
        _NUMBER_BOX,
        height / lines,
        act.number,
        border=1,
        align=Align.C,
        new_x=XPos.RIGHT,
        new_y=YPos.TOP,
    )
    pdf.set_xy(boxes + _NUMBER_BOX, top)
    pdf.cell(_NUMBER_BOX, height, f"{act.composed_on:%d.%m.%Y}", border=1, align=Align.C)
    pdf.set_xy(_MARGIN, top + height)


def _table(pdf: FPDF, act: ActDocument) -> None:
    """Восемь граф формы: две строки шапки, нумерация граф, строки акта и «Итого»."""
    pdf.set_font(_FAMILY, size=8)
    small = FontFace(size_pt=_SMALL)
    bold = FontFace(emphasis="BOLD")
    with pdf.table(
        col_widths=_COLUMNS,
        width=_WIDTH,
        align=Align.L,
        text_align=Align.C,
        line_height=4,
        padding=1,
        num_heading_rows=3,
        headings_style=small,
    ) as table:
        head = table.row()
        for text in form.COLUMN_HEADERS:
            head.cell(text, rowspan=2)
        head.cell(form.DONE_HEADER, colspan=3)
        done = table.row()
        for text in form.DONE_COLUMNS:
            done.cell(text)
        numbering = table.row()
        for position in range(1, 9):
            numbering.cell(str(position))
        for position, line in enumerate(act.lines, start=1):
            row = table.row()
            row.cell(str(position))
            row.cell(line.service, align=Align.L)
            row.cell(line.service_date)
            row.cell("")
            row.cell(line.unit)
            row.cell(quantity_text(line.quantity), align=Align.R)
            row.cell(money_text(line.price), align=Align.R)
            row.cell(money_text(line.cost), align=Align.R)
        total = table.row()
        total.cell(form.TOTAL, colspan=5, align=Align.L, style=bold)
        quantity = "" if act.quantity_total is None else quantity_text(act.quantity_total)
        total.cell(quantity, align=Align.R)
        total.cell(form.NO_PRICE)
        total.cell(money_text(act.total), align=Align.R, style=bold)


def _signatures(pdf: FPDF) -> None:
    """«Сдал»/«Принял» с подписью под линией, М.П., дата подписания и сноски формы."""
    if pdf.will_page_break(_TAIL_HEIGHT):
        pdf.add_page()
    half = _WIDTH / 2
    pdf.ln(5)
    pdf.set_font(_FAMILY, size=_BODY)
    labels = (form.HANDED_OVER, form.ACCEPTED)
    indents = [pdf.get_string_width(f"{label} ") for label in labels]
    pdf.cell(half, _LINE, f"{labels[0]} {form.SIGNATURE}")
    pdf.cell(half, _LINE, f"{labels[1]} {form.SIGNATURE}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(_FAMILY, size=_SMALL)
    for column, indent in enumerate(indents):
        pdf.set_x(_MARGIN + column * half + indent)
        pdf.cell(half - indent, _SMALL_LINE, form.SIGNATURE_CAPTION)
    pdf.ln(_SMALL_LINE + 3)
    pdf.set_font(_FAMILY, size=_BODY)
    pdf.cell(half, _LINE, form.STAMP)
    pdf.cell(half, _LINE, form.SIGNED_ON, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(_MARGIN + half)
    pdf.cell(half, _LINE, form.STAMP, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)
    pdf.set_font(_FAMILY, size=_SMALL)
    for footnote in form.FOOTNOTES:
        _paragraph(pdf, footnote, size=_SMALL, line=_SMALL_LINE)


def _paragraph(pdf: FPDF, text: str, *, size: float = _BODY, line: float = _LINE) -> None:
    """Текст во всю ширину страницы с переносом; курсор — на следующую строку."""
    pdf.set_font(_FAMILY, size=size)
    pdf.multi_cell(_WIDTH, line, text, align=Align.L, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _line_count(pdf: FPDF, width: float, text: str) -> int:
    """Сколько строк займёт текст в рамке этой ширины текущим шрифтом (не меньше одной)."""
    lines = pdf.multi_cell(width, _LINE, text, dry_run=True, output=MethodReturnValue.LINES)
    return max(len(lines), 1) if isinstance(lines, list) else 1


def money_text(value: Decimal) -> str:
    """«45 000,00» — неразрывный пробел между тысячами и запятая, как в кабинете (§4)."""
    return f"{value:,.2f}".replace(",", _NBSP).replace(".", ",")


def quantity_text(value: Decimal) -> str:
    """«2», «1,5», «1 250,125» — без лишних нулей после запятой."""
    return f"{value.normalize():,f}".replace(",", _NBSP).replace(".", ",")
