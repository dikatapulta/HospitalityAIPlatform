"""Журнал изменений у правки и «Проверено» (spec 0049 Р-10, ревью PR #510 Н-30).

Строка пишется той же транзакцией, что действие: откатилось действие — строки
нет. «Что» — заявка запроса (`#N`, его день, «Бухгалтерия», комната), значений
полей в строке нет. Пустая правка ничего не меняет; запрос без заявки
(брошенный) править нельзя — ERR-DOCUMENTS-004.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents import service
from hospitality.modules.documents.api import RequisitesStatus, RequisitesUpdate
from hospitality.modules.documents.tests.conftest import Hotels, make_acting_user, submit_form
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_actions
from hospitality.platform.staff_actions import StaffAction
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

BIN = "940140000385"
NEW_BIN = "971240001315"


async def _journal(hotels: Hotels) -> list[staff_actions.StaffActionRead]:
    with tenant_context(hotels.a.tenant_id):
        return await staff_actions.list_staff_actions(
            day_start=utc_now() - timedelta(days=1),
            day_end=utc_now() + timedelta(days=1),
            actor_login=None,
            actions=None,
            limit=50,
            offset=0,
        )


async def _submit(hotels: Hotels) -> documents_api.ActRequestRead:
    with tenant_context(hotels.a.tenant_id):
        return await documents_api.submit_act_request(
            submit_form(hotels.a, bill_to="ТОО Арман", note=f"БИН {BIN}"), []
        )


async def test_edit_and_check_write_journal_rows_without_values(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels)
    with tenant_context(hotels.a.tenant_id):
        await documents_api.update_requisites(
            read.id, RequisitesUpdate(bin_iin=NEW_BIN), acting_user=accountant
        )
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
        request = await requests_api.get_request(read.request_id)

    rows = await _journal(hotels)

    assert [row.action for row in rows] == [
        StaffAction.ACT_REQUISITES_CHECKED,
        StaffAction.ACT_REQUISITES_EDITED,
    ]
    for row in rows:
        assert row.request_id == read.request_id
        assert row.request_daily_number == request.daily_number
        assert row.request_service_day is not None
        assert row.request_service_day == request.service_day
        assert row.category_name == "Бухгалтерия"
        assert row.category_id == hotels.a.accounting_id
        assert row.room_number == "512"
        dumped = str(row.model_dump())
        assert NEW_BIN not in dumped and "Арман" not in dumped and "Айгуль" not in dumped


async def test_journal_row_rolls_back_with_the_edit(
    hotels: Hotels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н-30: строка журнала — в транзакции правки, а не после неё."""
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels)

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("after flush")

    monkeypatch.setattr(service, "_files_by_request", crash)
    with tenant_context(hotels.a.tenant_id), pytest.raises(RuntimeError):
        await documents_api.update_requisites(
            read.id, RequisitesUpdate(bin_iin=NEW_BIN), acting_user=accountant
        )
    monkeypatch.undo()
    with tenant_context(hotels.a.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - timedelta(days=1), created_before=utc_now() + timedelta(days=1)
        )

    assert await _journal(hotels) == []
    assert row.edited_fields == []


async def test_empty_edit_keeps_checked_and_writes_nothing(hotels: Hotels) -> None:
    accountant = await make_acting_user("Айгуль")
    read = await _submit(hotels)
    with tenant_context(hotels.a.tenant_id):
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
        same = await documents_api.update_requisites(
            read.id, RequisitesUpdate(), acting_user=accountant
        )

    assert same.status is RequisitesStatus.CHECKED
    assert [row.action for row in await _journal(hotels)] == [StaffAction.ACT_REQUISITES_CHECKED]


async def test_request_without_service_request_cannot_be_edited(hotels: Hotels) -> None:
    """Брошенный запрос (сбой до заявки) — ERR-DOCUMENTS-004, журнал пуст."""
    accountant = await make_acting_user("Айгуль")

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("process died")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(RuntimeError):
        patch.setattr(requests_api, "create_request", crash)
        await _submit(hotels)
    with tenant_context(hotels.a.tenant_id):
        (row,) = await documents_api.list_act_requests(
            created_from=utc_now() - timedelta(days=1), created_before=utc_now() + timedelta(days=1)
        )
        with pytest.raises(AppError) as edited:
            await documents_api.update_requisites(
                row.id, RequisitesUpdate(bin_iin=NEW_BIN), acting_user=accountant
            )
        with pytest.raises(AppError) as checked:
            await documents_api.mark_requisites_checked(row.id, acting_user=accountant)

    assert edited.value.code == checked.value.code == documents_api.ERR_DOCUMENTS_NOT_FOUND
    assert await _journal(hotels) == []
