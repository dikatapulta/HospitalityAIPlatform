"""Адаптер «Excel для 1С»: контрагенты файлом для загрузки в 1С (spec 0052, issue #517).

Упрощённый адаптер `AccountingPort` (FOUNDATION §8 правило 5): с 1С он не
соединяется. Бухгалтер скачивает файл со страницы «Документы для АВР» и
загружает его в 1С обработкой загрузки из табличного документа: колонки
сопоставляются один раз, настройку 1С запоминает. Порядок колонок — как в
карточке контрагента 1С и на странице (spec 0049 §6); номер и даты — в конце,
чтобы бухгалтер видел, к какому проживанию акт, и не сопоставлял их.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Final

from hospitality.integrations.accounting.xlsx import XLSX_CONTENT_TYPE, build_xlsx
from hospitality.shared.ports.accounting import CounterpartyRow, ExportFile

SHEET_NAME: Final = "Контрагенты"
HEADER: Final = ("Контрагент", "БИН/ИИН", "Адрес", "ИИК", "БИК", "Банк", "Номер", "Заезд", "Выезд")
# Ширина в знаках: название и адрес длинные, коды — по своей длине.
_WIDTHS: Final = (40, 14, 50, 24, 12, 30, 8, 11, 11)


class OneCXlsxAccountingAdapter:
    """Файл .xlsx: строка — контрагент, все ячейки — текст."""

    backend = "onec_xlsx"

    async def export_counterparties(
        self, rows: Sequence[CounterpartyRow], *, period_from: date, period_to: date
    ) -> ExportFile:
        table = [
            HEADER,
            *(
                (
                    row.company_name,
                    row.bin_iin,
                    row.address,
                    row.iik,
                    row.bik,
                    row.bank_name,
                    row.room_number,
                    f"{row.check_in:%d.%m.%Y}",
                    f"{row.check_out:%d.%m.%Y}",
                )
                for row in rows
            ),
        ]
        return ExportFile(
            filename=f"Контрагенты для 1С {period_from:%d.%m.%Y}–{period_to:%d.%m.%Y}.xlsx",
            content_type=XLSX_CONTENT_TYPE,
            content=build_xlsx(SHEET_NAME, table, widths=_WIDTHS),
        )

    async def check(self) -> bool:
        """Внешней системы нет — готов всегда."""
        return True
