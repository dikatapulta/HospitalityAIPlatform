"""Выгрузка проверенных реквизитов для учётной системы (spec 0052, issue #517).

Только строки «Проверено» за период: в 1С уходит то, что сверил человек (spec 0049
Р-5), а не черновик разбора. Обезличенные строки (spec 0032) и запросы, которые
бухгалтерия вернула гостю, в файл не идут: у первых значений нет, вторые гость
пришлёт заново. Формат файла — адаптер порта `AccountingPort`: модуль знает
только строки. Выгрузка ничего не меняет, поэтому в журнал изменений (spec 0041)
не пишется — след в логе с тем, кто выгрузил, как у выдачи файла гостя.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, tzinfo
from typing import Final

from pydantic import BaseModel

from hospitality.modules.documents.schemas import ActingUser, RequisitesStatus
from hospitality.modules.documents.service import list_act_requests
from hospitality.modules.requests import api as requests_api
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_act_requisites_export
from hospitality.shared.ports.accounting import CounterpartyRow, ExportFile, get_accounting

logger = get_logger(module=__name__)

# Потолок строк файла: строка живёт и после обезличивания, длинный период без
# потолка читал бы все. Больше — «сузьте даты», а не обрезанный молча файл.
EXPORT_LIMIT: Final = 1000


class ExportOutcome(enum.StrEnum):
    EXPORTED = "exported"
    EMPTY = "empty"  # строк «Проверено» за период нет
    TOO_MANY = "too_many"  # больше `EXPORT_LIMIT`


class RequisitesExport(BaseModel):
    """Исход выгрузки: файл есть только у `exported`."""

    outcome: ExportOutcome
    rows: int = 0
    file: ExportFile | None = None


async def export_checked_requisites(
    *,
    created_from: datetime,
    created_before: datetime,
    period_from: date,
    period_to: date,
    zone: tzinfo,
    acting_user: ActingUser,
) -> RequisitesExport:
    """Файл проверенных реквизитов запросов, созданных в окне; новые сверху, как на странице.

    `created_*` — окно в UTC, `period_*` — те же дни отеля для имени файла,
    `zone` — пояс отеля для дат заезда и выезда. Ключа шифрования нет —
    ERR-FILES-005 от `list_act_requests`, как у страницы.
    """
    rows = await list_act_requests(
        created_from=created_from,
        created_before=created_before,
        status=RequisitesStatus.CHECKED,
        limit=EXPORT_LIMIT + 1,
    )
    if len(rows) > EXPORT_LIMIT:
        return _done(ExportOutcome.TOO_MANY, acting_user)
    statuses = {
        request.id: request.status
        for request in await requests_api.list_requests_by_ids([row.request_id for row in rows])
    }
    counterparties = [
        CounterpartyRow(
            company_name=row.requisites.company_name,
            bin_iin=row.requisites.bin_iin,
            address=row.requisites.address,
            iik=row.requisites.iik,
            bik=row.requisites.bik,
            bank_name=row.requisites.bank_name,
            room_number=row.room_number,
            check_in=row.check_in_at.astimezone(zone).date(),
            check_out=row.check_out_at.astimezone(zone).date(),
        )
        for row in rows
        if not row.anonymized
        and statuses.get(row.request_id) not in (None, requests_api.RequestStatus.CANCELLED)
    ]
    if not counterparties:
        return _done(ExportOutcome.EMPTY, acting_user)
    accounting = get_accounting()
    exported = await accounting.export_counterparties(
        counterparties, period_from=period_from, period_to=period_to
    )
    return _done(
        ExportOutcome.EXPORTED,
        acting_user,
        rows=len(counterparties),
        file=exported,
        backend=accounting.backend,
    )


def _done(
    outcome: ExportOutcome,
    acting_user: ActingUser,
    *,
    rows: int = 0,
    file: ExportFile | None = None,
    backend: str | None = None,
) -> RequisitesExport:
    # Значений полей в логе нет: только сколько строк и кто выгрузил.
    logger.info(
        "act_requisites_exported",
        outcome=outcome.value,
        rows=rows,
        backend=backend,
        user_id=str(acting_user.user_id),
    )
    record_act_requisites_export(outcome.value)
    return RequisitesExport(outcome=outcome, rows=rows, file=file)
