"""Черновик АВР по форме Р-1 листом Excel (spec 0086 Р-1, Р-7).

Раскладка повторяет форму Р-1 — Приложение 50 к приказу Министра финансов РК
от 20.12.2012 № 562 (в редакции приказа № 458 от 27.10.2014): стороны с ИИН/БИН,
договор, номер и дата, таблица из восьми граф с «Итого», запасы, приложение,
«Сдал»/«Принял», М.П., дата подписания и три сноски. Подпись, печать и дату
подписания ставят люди на бумаге — здесь только линии под них.

Значения — ровно из снимка `ActDocument`: адаптер ничего не считает. Количество,
цена и стоимость — числа (стоимость — денежным форматом), всё прочее — текст.
Надписи формы и имя файла — `act_form.py`, общие с PDF.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from hospitality.integrations.accounting import act_form as form
from hospitality.integrations.accounting.act_form import act_filename
from hospitality.integrations.accounting.xlsx import (
    XLSX_CONTENT_TYPE,
    Cell,
    CellStyle,
    build_sheet,
)
from hospitality.shared.ports.accounting import ActDocument, ExportFile

SHEET_NAME: Final = "АВР"
# Ширина граф 1–8 в знаках: наименование и сведения об отчёте — длинные.
_WIDTHS: Final = (8, 44, 16, 22, 11, 11, 15, 17)

_PLAIN: Final = CellStyle()
_BOLD: Final = CellStyle(bold=True)
_WRAP: Final = CellStyle(wrap=True)
_BOX: Final = CellStyle(border=True, center=True, wrap=True)
_BOX_TEXT: Final = CellStyle(border=True, wrap=True)
_BOX_NUMBER: Final = CellStyle(border=True)
_BOX_MONEY: Final = CellStyle(border=True, money=True)
_BOX_TOTAL: Final = CellStyle(border=True, bold=True, money=True)
_TITLE: Final = CellStyle(bold=True, center=True)


def render_act_xlsx(act: ActDocument) -> ExportFile:
    """Лист формы Р-1 из снимка: тот же снимок — те же байты."""
    rows: list[Sequence[Cell | None]] = []
    merges: list[str] = []
    heights: dict[int, int] = {}

    def add(cells: Sequence[Cell | None], *, merge: str | None = None, height: int = 0) -> int:
        rows.append(cells)
        number = len(rows)
        if merge:
            merges.append(merge.format(n=number))
        if height:
            heights[number] = height
        return number

    add([None, None, None, None, Cell(form.FORM_SOURCE, _WRAP)], merge="E{n}:H{n}", height=32)
    add([None] * 7 + [Cell(form.FORM_NAME, _PLAIN)])
    add([])
    for label, party in (("Заказчик", act.customer), ("Исполнитель", act.executor)):
        add(
            [
                Cell(form.party_line(label, party), _WRAP),
                *[None] * 5,
                Cell(form.BIN_IIN, _BOX),
                Cell(party.bin_iin, _BOX),
            ],
            merge="A{n}:F{n}",
            height=32,
        )
    add([Cell(form.contract_line(act), _PLAIN)])
    add([])
    add([None] * 6 + [Cell(form.DOCUMENT_NUMBER, _BOX), Cell(form.COMPOSED_ON, _BOX)], height=30)
    add(
        [
            Cell(form.TITLE, _TITLE),
            *[None] * 5,
            Cell(act.number, _BOX),
            Cell(f"{act.composed_on:%d.%m.%Y}", _BOX),
        ],
        merge="A{n}:F{n}",
    )
    add([])
    head = add(
        [Cell(text, _BOX) for text in form.COLUMN_HEADERS] + [Cell(form.DONE_HEADER, _BOX)],
        height=30,
    )
    add([None] * 5 + [Cell(text, _BOX) for text in form.DONE_COLUMNS], height=80)
    merges.extend(f"{column}{head}:{column}{head + 1}" for column in "ABCDE")
    merges.append(f"F{head}:H{head}")
    add([Cell(str(n), _BOX) for n in range(1, 9)])
    for position, line in enumerate(act.lines, start=1):
        add(
            [
                Cell(str(position), _BOX),
                Cell(line.service, _BOX_TEXT),
                Cell(line.service_date, _BOX),
                Cell("", _BOX),
                Cell(line.unit, _BOX),
                Cell(line.quantity, _BOX_NUMBER),
                Cell(line.price, _BOX_MONEY),
                Cell(line.cost, _BOX_MONEY),
            ]
        )
    quantity: Cell = (
        Cell(act.quantity_total, _BOX_NUMBER) if act.quantity_total is not None else Cell("", _BOX)
    )
    add(
        [
            Cell(form.TOTAL, CellStyle(border=True, bold=True)),
            Cell("", _BOX),
            Cell("", _BOX),
            Cell("", _BOX),
            Cell("", _BOX),
            quantity,
            Cell(form.NO_PRICE, _BOX),
            Cell(act.total, _BOX_TOTAL),
        ],
        merge="A{n}:E{n}",
    )
    add([])
    add([Cell(form.SUPPLIES, _PLAIN)])
    add(
        [Cell(form.ATTACHMENT, _WRAP)],
        merge="A{n}:H{n}",
        height=32,
    )
    add([])
    add(
        [
            Cell(f"{form.HANDED_OVER} {form.SIGNATURE}", _PLAIN),
            None,
            None,
            None,
            Cell(f"{form.ACCEPTED} {form.SIGNATURE}", _PLAIN),
        ],
        merge="A{n}:D{n}",
    )
    merges.append(f"E{len(rows)}:H{len(rows)}")
    caption = Cell(form.SIGNATURE_CAPTION, _PLAIN)
    add([caption, None, None, None, caption])
    add([])
    add(
        [
            Cell(form.STAMP, _PLAIN),
            None,
            None,
            None,
            Cell(form.SIGNED_ON, _PLAIN),
        ]
    )
    add([None] * 4 + [Cell(form.STAMP, _PLAIN)])
    add([])
    # Вторая сноска — в две строки, прочие — в одну.
    for footnote, height in zip(form.FOOTNOTES, (0, 30, 0), strict=True):
        add([Cell(footnote, _WRAP)], merge="A{n}:H{n}", height=height)
    return ExportFile(
        filename=act_filename(act, "xlsx"),
        content_type=XLSX_CONTENT_TYPE,
        content=build_sheet(
            SHEET_NAME, rows, widths=_WIDTHS, merges=merges, heights=heights, landscape=True
        ),
    )
