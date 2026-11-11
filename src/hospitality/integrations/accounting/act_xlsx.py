"""Черновик АВР по форме Р-1 листом Excel (spec 0086 Р-1, Р-7).

Раскладка повторяет форму Р-1 — Приложение 50 к приказу Министра финансов РК
от 20.12.2012 № 562 (в редакции приказа № 458 от 27.10.2014): стороны с ИИН/БИН,
договор, номер и дата, таблица из восьми граф с «Итого», запасы, приложение,
«Сдал»/«Принял», М.П., дата подписания и три сноски. Подпись, печать и дату
подписания ставят люди на бумаге — здесь только линии под них.

Значения — ровно из снимка `ActDocument`: адаптер ничего не считает. Количество,
цена и стоимость — числа (стоимость — денежным форматом), всё прочее — текст.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from hospitality.integrations.accounting.xlsx import (
    XLSX_CONTENT_TYPE,
    Cell,
    CellStyle,
    build_sheet,
)
from hospitality.shared.ports.accounting import ActDocument, ActParty, ExportFile

SHEET_NAME: Final = "АВР"
TITLE: Final = "АКТ ВЫПОЛНЕННЫХ РАБОТ (ОКАЗАННЫХ УСЛУГ)*"
FORM_SOURCE: Final = (
    "Приложение 50 к приказу Министра финансов Республики Казахстан от 20 декабря 2012 года № 562"
)
COLUMN_HEADERS: Final = (
    "Номер по порядку",
    "Наименование работ (услуг) (в разрезе их подвидов в соответствии с технической "
    "спецификацией, заданием, графиком выполнения работ (услуг) при их наличии)",
    "Дата выполнения работ (оказания услуг)**",
    "Сведения об отчете о научных исследованиях, маркетинговых, консультационных и прочих "
    "услугах (дата, номер, количество страниц) (при их наличии)***",
    "Единица измерения",
)
DONE_HEADER: Final = "Выполнено работ (оказано услуг)"
DONE_COLUMNS: Final = ("количество", "цена за единицу", "стоимость")
FOOTNOTES: Final = (
    "*Применяется для приемки-передачи выполненных работ (оказанных услуг), за исключением "
    "строительно-монтажных работ.",
    "**Заполняется в случае, если даты выполненных работ (оказанных услуг) приходятся на "
    "различные периоды, а также в случае, если даты выполнения работ (оказания услуг) и даты "
    "подписания (принятия) работ (услуг) различны.",
    "***Заполняется в случае наличия отчета о научных исследованиях, маркетинговых, "
    "консультационных и прочих услугах.",
)
_BLANK_CONTRACT: Final = "___ № ___ от «___» __________ 20__ г."
_SIGNATURE: Final = "____________ / ____________ / ______________"
_SIGNATURE_CAPTION: Final = "должность / подпись / расшифровка подписи"
# Ширина граф 1–8 в знаках: наименование и сведения об отчёте — длинные.
_WIDTHS: Final = (8, 44, 16, 22, 11, 11, 15, 17)
# Знаки, которых не вынесет имя файла в «Сохранить как» на Windows и macOS.
_FILENAME_UNSAFE: Final = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_PLAIN: Final = CellStyle()
_BOLD: Final = CellStyle(bold=True)
_WRAP: Final = CellStyle(wrap=True)
_BOX: Final = CellStyle(border=True, center=True, wrap=True)
_BOX_TEXT: Final = CellStyle(border=True, wrap=True)
_BOX_NUMBER: Final = CellStyle(border=True)
_BOX_MONEY: Final = CellStyle(border=True, money=True)
_BOX_TOTAL: Final = CellStyle(border=True, bold=True, money=True)
_TITLE: Final = CellStyle(bold=True, center=True)


def act_filename(act: ActDocument, extension: str) -> str:
    """«АВР № 15 от 11.11.2026.xlsx»; знаки, недопустимые в имени файла, — дефис."""
    number = _FILENAME_UNSAFE.sub("-", act.number)
    return f"АВР № {number} от {act.composed_on:%d.%m.%Y}.{extension}"


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

    add([None, None, None, None, Cell(FORM_SOURCE, _WRAP)], merge="E{n}:H{n}", height=32)
    add([None] * 7 + [Cell("Форма Р-1", _PLAIN)])
    add([])
    for label, party in (("Заказчик", act.customer), ("Исполнитель", act.executor)):
        add(
            [
                Cell(f"{label}: {_party_text(party)}", _WRAP),
                *[None] * 5,
                Cell("ИИН/БИН", _BOX),
                Cell(party.bin_iin, _BOX),
            ],
            merge="A{n}:F{n}",
            height=32,
        )
    add([Cell(f"Договор (контракт): {act.contract or _BLANK_CONTRACT}", _PLAIN)])
    add([])
    add([None] * 6 + [Cell("Номер документа", _BOX), Cell("Дата составления", _BOX)], height=30)
    add(
        [
            Cell(TITLE, _TITLE),
            *[None] * 5,
            Cell(act.number, _BOX),
            Cell(f"{act.composed_on:%d.%m.%Y}", _BOX),
        ],
        merge="A{n}:F{n}",
    )
    add([])
    head = add([Cell(text, _BOX) for text in COLUMN_HEADERS] + [Cell(DONE_HEADER, _BOX)], height=30)
    add([None] * 5 + [Cell(text, _BOX) for text in DONE_COLUMNS], height=80)
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
            Cell("Итого", CellStyle(border=True, bold=True)),
            Cell("", _BOX),
            Cell("", _BOX),
            Cell("", _BOX),
            Cell("", _BOX),
            quantity,
            Cell("х", _BOX),
            Cell(act.total, _BOX_TOTAL),
        ],
        merge="A{n}:E{n}",
    )
    add([])
    add(
        [
            Cell(
                "Сведения об использовании запасов, полученных от заказчика: "
                "_____________________________ (наименование, количество, стоимость)",
                _PLAIN,
            )
        ]
    )
    add(
        [
            Cell(
                "Приложение: Перечень документации, в том числе отчет(ы) о маркетинговых, научных "
                "исследованиях, консультационных и прочих услугах (обязательны при его (их) "
                "наличии) на _______ страниц",
                _WRAP,
            )
        ],
        merge="A{n}:H{n}",
        height=32,
    )
    add([])
    add(
        [
            Cell(f"Сдал (Исполнитель) {_SIGNATURE}", _PLAIN),
            None,
            None,
            None,
            Cell(f"Принял (Заказчик) {_SIGNATURE}", _PLAIN),
        ],
        merge="A{n}:D{n}",
    )
    merges.append(f"E{len(rows)}:H{len(rows)}")
    add([Cell(_SIGNATURE_CAPTION, _PLAIN), None, None, None, Cell(_SIGNATURE_CAPTION, _PLAIN)])
    add([])
    add(
        [
            Cell("М.П.", _PLAIN),
            None,
            None,
            None,
            Cell("Дата подписания (принятия) работ (услуг) ________________", _PLAIN),
        ]
    )
    add([None] * 4 + [Cell("М.П.", _PLAIN)])
    add([])
    # Вторая сноска — в две строки, прочие — в одну.
    for footnote, height in zip(FOOTNOTES, (0, 30, 0), strict=True):
        add([Cell(footnote, _WRAP)], merge="A{n}:H{n}", height=height)
    return ExportFile(
        filename=act_filename(act, "xlsx"),
        content_type=XLSX_CONTENT_TYPE,
        content=build_sheet(
            SHEET_NAME, rows, widths=_WIDTHS, merges=merges, heights=heights, landscape=True
        ),
    )


def _party_text(party: ActParty) -> str:
    """«полное наименование, адрес, данные о средствах связи» — пустое пропускается."""
    return ", ".join(part for part in (party.name, party.address, party.contact) if part)
