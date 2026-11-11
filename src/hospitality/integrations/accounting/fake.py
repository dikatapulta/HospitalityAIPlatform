"""Fake-адаптер учётной системы (ADR-007): справочник контрагентов в памяти.

Ведёт себя как 1С, в которую выгрузили файл: контрагент заводится по БИН/ИИН
(без него — по названию), повторная выгрузка того же БИН обновляет карточку, а
не плодит вторую. Файл — JSON той же таблицы, что у Excel-адаптера: тестам и
демо его легко прочитать. Черновик АВР (spec 0086) — JSON снимка `ActDocument`
под тем же именем, что у Excel. Для тестов, CI и локального запуска.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date

from hospitality.integrations.accounting.act_xlsx import act_filename
from hospitality.integrations.accounting.xlsx import clean_cell_text
from hospitality.shared.ports.accounting import (
    ActDocument,
    ActFileFormat,
    CounterpartyRow,
    ExportFile,
)

FIELDS = (
    "company_name",
    "bin_iin",
    "address",
    "iik",
    "bik",
    "bank_name",
    "room_number",
    "check_in",
    "check_out",
)


class FakeAccountingAdapter:
    """`counterparties` — справочник «1С», `exports` — выданные файлы по порядку,
    `acts` — собранные черновики АВР по порядку."""

    backend = "fake"

    def __init__(self) -> None:
        self.counterparties: dict[str, CounterpartyRow] = {}
        self.exports: list[ExportFile] = []
        self.acts: list[ActDocument] = []

    async def export_counterparties(
        self, rows: Sequence[CounterpartyRow], *, period_from: date, period_to: date
    ) -> ExportFile:
        table = [
            [clean_cell_text(str(value)) for value in row.model_dump(mode="json").values()]
            for row in rows
        ]
        for row in rows:
            self.counterparties[row.bin_iin or row.company_name] = row
        exported = ExportFile(
            filename=f"counterparties-{period_from.isoformat()}-{period_to.isoformat()}.json",
            content_type="application/json",
            content=json.dumps({"header": FIELDS, "rows": table}, ensure_ascii=False).encode(),
        )
        self.exports.append(exported)
        return exported

    async def render_act(self, act: ActDocument, *, file_format: ActFileFormat) -> ExportFile:
        """JSON снимка; формат пишется в файл — тест видит, какой просили."""
        self.acts.append(act)
        payload = {"format": file_format.value, **act.model_dump(mode="json")}
        return ExportFile(
            filename=act_filename(act, "json"),
            content_type="application/json",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
        )

    async def check(self) -> bool:
        return True
