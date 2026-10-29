"""Выгрузка проверенных реквизитов для 1С (spec 0052, issue #517).

В файл идут только строки «Проверено» своего отеля за окно: не проверенная,
обезличенная и возвращённая гостю — нет; новые сверху; даты — дни отеля. Нет
строк — исхода `empty` без вызова порта; больше потолка — `too_many`. Файл —
от Fake-адаптера порта: JSON той же таблицы.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.api import ActingUser, ExportOutcome, RequisitesUpdate
from hospitality.modules.documents.tests.conftest import (
    Hotel,
    Hotels,
    make_acting_user,
    submit_form,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.db import utc_now
from hospitality.shared.ports.accounting import AccountingPort
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_accounting  # noqa: F401  (фикстура для pytest)

BIN = "940140000385"
NEW_BIN = "971240001315"
ALMATY = ZoneInfo("Asia/Almaty")


async def _checked(hotel: Hotel, accountant: ActingUser, bill_to: str, bin_iin: str) -> None:
    with tenant_context(hotel.tenant_id):
        read = await documents_api.submit_act_request(
            submit_form(hotel, bill_to=bill_to, note=f"БИН {bin_iin}"), []
        )
        await documents_api.extract_requisites(read.id)
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)


async def _export(hotel: Hotel, accountant: ActingUser) -> documents_api.RequisitesExport:
    now = utc_now()
    with tenant_context(hotel.tenant_id):
        return await documents_api.export_checked_requisites(
            created_from=now - timedelta(days=1),
            created_before=now + timedelta(days=1),
            period_from=now.date() - timedelta(days=1),
            period_to=now.date(),
            zone=ALMATY,
            acting_user=accountant,
        )


def _table(result: documents_api.RequisitesExport) -> list[list[str]]:
    assert result.file is not None
    rows: list[list[str]] = json.loads(result.file.content)["rows"]
    return rows


async def test_only_checked_rows_of_own_hotel_newest_first(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    accountant = await make_acting_user("Айгуль")
    await _checked(hotels.a, accountant, "ТОО Арман", BIN)
    await _checked(hotels.a, accountant, "ТОО Береке", NEW_BIN)
    await _checked(hotels.b, accountant, "ТОО Чужой", BIN)
    with tenant_context(hotels.a.tenant_id):
        await documents_api.submit_act_request(
            submit_form(hotels.a, bill_to="ТОО Непроверенный", note=f"БИН {BIN}"), []
        )

    result = await _export(hotels.a, accountant)

    assert result.outcome is ExportOutcome.EXPORTED and result.rows == 2
    table = _table(result)
    assert [(row[0], row[1], row[6]) for row in table] == [
        ("ТОО Береке", NEW_BIN, "512"),
        ("ТОО Арман", BIN, "512"),
    ]


async def test_dates_are_hotel_days(hotels: Hotels, fake_accounting: AccountingPort) -> None:
    accountant = await make_acting_user("Айгуль")
    await _checked(hotels.a, accountant, "ТОО Арман", BIN)
    with tenant_context(hotels.a.tenant_id):
        stay = (
            await documents_api.list_act_requests(
                created_from=utc_now() - timedelta(days=1),
                created_before=utc_now() + timedelta(days=1),
            )
        )[0]

    (row,) = _table(await _export(hotels.a, accountant))

    assert row[7] == stay.check_in_at.astimezone(ALMATY).date().isoformat()
    assert row[8] == stay.check_out_at.astimezone(ALMATY).date().isoformat()


async def test_edited_after_check_returned_and_anonymized_rows_stay_out(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    """Правка снимает «Проверено» (Р-5); возвращённый гостю запрос гость пришлёт заново;
    у обезличенной строки значений нет (spec 0032)."""
    accountant = await make_acting_user("Айгуль")
    await _checked(hotels.a, accountant, "ТОО Арман", BIN)
    with tenant_context(hotels.a.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - timedelta(days=1), created_before=utc_now() + timedelta(days=1)
        )
        await documents_api.update_requisites(
            row.id, RequisitesUpdate(bin_iin=NEW_BIN), acting_user=accountant
        )
    edited = await _export(hotels.a, accountant)

    with tenant_context(hotels.a.tenant_id):
        await documents_api.mark_requisites_checked(row.id, acting_user=accountant)
        await requests_api.change_request_status(
            row.request_id, requests_api.RequestStatus.CANCELLED, resolution_note="Нужен ИИК"
        )
    returned = await _export(hotels.a, accountant)

    await _checked(hotels.a, accountant, "ТОО Береке", BIN)
    with tenant_context(hotels.a.tenant_id):
        await documents_api.anonymize_expired_act_requests(
            created_before=utc_now() + timedelta(seconds=1)
        )
    anonymized = await _export(hotels.a, accountant)

    assert edited.outcome is ExportOutcome.EMPTY and edited.file is None
    assert returned.outcome is ExportOutcome.EMPTY
    assert anonymized.outcome is ExportOutcome.EMPTY


async def test_more_than_the_limit_asks_to_narrow_dates(
    hotels: Hotels, fake_accounting: AccountingPort, monkeypatch: pytest.MonkeyPatch
) -> None:
    accountant = await make_acting_user("Айгуль")
    await _checked(hotels.a, accountant, "ТОО Арман", BIN)
    await _checked(hotels.a, accountant, "ТОО Береке", NEW_BIN)
    monkeypatch.setattr("hospitality.modules.documents.export.EXPORT_LIMIT", 1)

    result = await _export(hotels.a, accountant)

    assert result.outcome is ExportOutcome.TOO_MANY and result.file is None
