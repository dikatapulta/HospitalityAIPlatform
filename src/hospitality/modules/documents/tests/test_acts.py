"""Черновик АВР по кнопке бухгалтера (spec 0086, issue #953).

Акт — только у строки «Проверено» своего отеля, не возвращённой гостю и не
обезличенной (007/004); без исполнителя в конфиге — 008. В акт ложится снимок:
правка строки после акта его не меняет. Стоимость и «Итого» — с округлением до
тиына; снимок — шифртекстом под своим AAD; журнал — без значений; обезличивание
запроса обезличивает и его акты. Файл — от Fake-адаптера порта: JSON снимка.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.api import (
    ActingUser,
    RequisitesUpdate,
    WorkCompletionActCreate,
)
from hospitality.modules.documents.tests.conftest import (
    Hotel,
    Hotels,
    make_acting_user,
    submit_form,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_actions
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.field_encryption import decrypt_field, field_aad
from hospitality.platform.staff_actions import StaffAction
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.ports.accounting import AccountingPort, ActFileFormat
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_accounting  # noqa: F401  (фикстура для pytest)

BIN = "940140000385"
NEW_BIN = "971240001315"
EXECUTOR = {
    "name": "ТОО «Отель Тест»",
    "bin_iin": "123456789012",
    "address": "г. Алматы, пр. Абая 1",
    "contact": "+7 727 000 00 00",
}


async def _configure(hotel: Hotel, *, executor: dict[str, str] | None = EXECUTOR) -> None:
    config = TenantConfig.model_validate(
        {
            "profile": HotelProfile(city="Almaty", country_code="KZ"),
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "act_executor": executor,
        }
    )
    async with platform_session_scope() as session:
        await store_tenant_config(session, hotel.tenant_id, config)


async def _checked_row(hotel: Hotel, accountant: ActingUser) -> documents_api.ActRequestRow:
    with tenant_context(hotel.tenant_id):
        read = await documents_api.submit_act_request(
            submit_form(hotel, bill_to="ТОО Арман", note=f"БИН {BIN}, адрес: Алматы, Абая 10"), []
        )
        await documents_api.extract_requisites(read.id)
        return await documents_api.mark_requisites_checked(read.id, acting_user=accountant)


def _form(*lines: dict[str, object], number: str = "15") -> WorkCompletionActCreate:
    return WorkCompletionActCreate.model_validate(
        {
            "number": number,
            "composed_on": "2026-11-11",
            "contract": "№ 12 от 01.11.2026",
            "lines": list(lines)
            or [
                {
                    "service": "Проживание, номер 512",
                    "unit": "сутки",
                    "quantity": "2",
                    "price": "22500",
                }
            ],
        }
    )


async def _create(
    hotel: Hotel, act_request_id: uuid.UUID, accountant: ActingUser, form: WorkCompletionActCreate
) -> documents_api.WorkCompletionActRead:
    with tenant_context(hotel.tenant_id):
        return await documents_api.create_work_completion_act(
            act_request_id, form, acting_user=accountant
        )


async def _file(hotel: Hotel, act_id: uuid.UUID, accountant: ActingUser) -> dict[str, object]:
    with tenant_context(hotel.tenant_id):
        rendered = await documents_api.render_work_completion_act(
            act_id, ActFileFormat.XLSX, acting_user=accountant
        )
    assert rendered.filename.startswith("АВР № ")
    payload: dict[str, object] = json.loads(rendered.content)
    return payload


async def test_checked_row_gives_act_with_snapshot_of_both_parties(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)

    act = await _create(hotels.a, row.id, accountant, _form())
    payload = await _file(hotels.a, act.id, accountant)

    assert (act.number, act.composed_on, act.total) == ("15", date(2026, 11, 11), Decimal("45000"))
    assert act.created_by_display_name == "Айгуль" and not act.anonymized
    assert payload["format"] == "xlsx" and payload["contract"] == "№ 12 от 01.11.2026"
    assert payload["customer"] == {
        "name": "ТОО Арман",
        "bin_iin": BIN,
        "address": row.requisites.address,
        "contact": "",
    }
    assert payload["executor"] == EXECUTOR
    assert payload["total"] == "45000.00" and payload["quantity_total"] == "2"
    with tenant_context(hotels.a.tenant_id):
        listed = await documents_api.list_work_completion_acts([row.id, uuid.uuid4()])
    assert listed == {row.id: [act]}


async def test_cost_and_total_round_to_tiyn_and_mixed_units_have_no_quantity_total(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)
    form = _form(
        {"service": "Проживание", "unit": "сутки", "quantity": "1.5", "price": "333.33"},
        {
            "service": "Завтрак",
            "service_date": "11.11.2026",
            "unit": "услуга",
            "quantity": "0.5",
            "price": "0.25",
        },
    )

    act = await _create(hotels.a, row.id, accountant, form)
    payload = await _file(hotels.a, act.id, accountant)

    lines = payload["lines"]
    assert isinstance(lines, list)
    # 1,5 × 333,33 = 499,995 → 500,00; 0,5 × 0,25 = 0,125 → 0,13 (half-up, не банковское 0,12).
    assert [line["cost"] for line in lines] == ["500.00", "0.13"]
    assert act.total == Decimal("500.13") and payload["quantity_total"] is None


@pytest.mark.parametrize("step", ["to_check", "edited", "returned"])
async def test_act_needs_checked_requisites_of_a_live_request(
    hotels: Hotels, fake_accounting: AccountingPort, step: str
) -> None:
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    with tenant_context(hotels.a.tenant_id):
        read = await documents_api.submit_act_request(
            submit_form(hotels.a, bill_to="ТОО Арман", note=f"БИН {BIN}"), []
        )
        await documents_api.extract_requisites(read.id)
        if step != "to_check":
            await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
        if step == "edited":
            await documents_api.update_requisites(
                read.id, RequisitesUpdate(bin_iin=NEW_BIN), acting_user=accountant
            )
        if step == "returned":
            await requests_api.change_request_status(
                read.request_id, requests_api.RequestStatus.CANCELLED, resolution_note="Нужен ИИК"
            )

    with pytest.raises(AppError) as rejected:
        await _create(hotels.a, read.id, accountant, _form())

    assert rejected.value.code == documents_api.ERR_DOCUMENTS_NOT_CHECKED
    assert rejected.value.status_code == 409


async def test_no_executor_in_config_is_008(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a, executor=None)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)

    with pytest.raises(AppError) as rejected:
        await _create(hotels.a, row.id, accountant, _form())

    assert rejected.value.code == documents_api.ERR_DOCUMENTS_NO_EXECUTOR


async def test_snapshot_survives_later_edit_of_the_row(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    """Правка после акта снимает «Проверено» — новый акт ждёт проверки, прежний неизменен."""
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)
    act = await _create(hotels.a, row.id, accountant, _form())
    with tenant_context(hotels.a.tenant_id):
        await documents_api.update_requisites(
            row.id, RequisitesUpdate(bin_iin=NEW_BIN), acting_user=accountant
        )

    payload = await _file(hotels.a, act.id, accountant)
    with pytest.raises(AppError) as again:
        await _create(hotels.a, row.id, accountant, _form(number="16"))

    customer = payload["customer"]
    assert isinstance(customer, dict) and customer["bin_iin"] == BIN
    assert again.value.code == documents_api.ERR_DOCUMENTS_NOT_CHECKED


async def test_snapshot_is_ciphertext_under_its_own_address(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)
    act = await _create(hotels.a, row.id, accountant, _form())

    with tenant_context(hotels.a.tenant_id):
        async with session_scope() as session:
            (blob,) = (
                await session.execute(text("SELECT content_ciphertext FROM work_completion_acts"))
            ).one()
    aad = field_aad(hotels.a.tenant_id, "work_completion_acts", act.id, "content_ciphertext")
    foreign = field_aad(
        hotels.a.tenant_id, "work_completion_acts", uuid.uuid4(), "content_ciphertext"
    )

    assert BIN.encode() not in blob and "Арман".encode() not in blob
    assert BIN in decrypt_field(blob, aad)
    with pytest.raises(AppError):
        decrypt_field(blob, foreign)


async def test_other_hotel_sees_neither_request_nor_act(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a)
    await _configure(hotels.b)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)
    act = await _create(hotels.a, row.id, accountant, _form())

    with pytest.raises(AppError) as create:
        await _create(hotels.b, row.id, accountant, _form())
    with pytest.raises(AppError) as render, tenant_context(hotels.b.tenant_id):
        await documents_api.render_work_completion_act(
            act.id, ActFileFormat.XLSX, acting_user=accountant
        )
    with tenant_context(hotels.b.tenant_id):
        listed = await documents_api.list_work_completion_acts([row.id])

    assert create.value.code == render.value.code == documents_api.ERR_DOCUMENTS_NOT_FOUND
    assert listed == {}


async def test_journal_says_act_created_without_values(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)
    await _create(hotels.a, row.id, accountant, _form())

    with tenant_context(hotels.a.tenant_id):
        journal = await staff_actions.list_staff_actions(
            day_start=utc_now() - timedelta(days=1),
            day_end=utc_now() + timedelta(days=1),
            actor_login=None,
            actions=None,
            limit=50,
            offset=0,
        )

    created = [
        entry for entry in journal if entry.action is StaffAction.WORK_COMPLETION_ACT_CREATED
    ]
    assert len(created) == 1
    assert created[0].request_id == row.request_id and created[0].room_number == "512"
    assert BIN not in created[0].model_dump_json() and "Арман" not in created[0].model_dump_json()


async def test_anonymizing_the_request_anonymizes_its_acts(
    hotels: Hotels, fake_accounting: AccountingPort
) -> None:
    await _configure(hotels.a)
    accountant = await make_acting_user("Айгуль")
    row = await _checked_row(hotels.a, accountant)
    act = await _create(hotels.a, row.id, accountant, _form())

    with tenant_context(hotels.a.tenant_id):
        await documents_api.anonymize_expired_act_requests(
            created_before=utc_now() + timedelta(seconds=1)
        )
        (listed,) = (await documents_api.list_work_completion_acts([row.id]))[row.id]
    with pytest.raises(AppError) as render:
        await _file(hotels.a, act.id, accountant)

    assert listed.anonymized and listed.created_by_display_name is None
    assert (listed.number, listed.total) == ("15", Decimal("45000.00"))
    assert render.value.code == documents_api.ERR_DOCUMENTS_NOT_FOUND


@pytest.mark.parametrize(
    "change",
    [
        {"number": ""},
        {"number": "1\n2"},
        {"lines": []},
        {"lines": [{"service": "x", "unit": "сутки", "quantity": "0", "price": "1"}]},
        {"lines": [{"service": "x", "unit": "сутки", "quantity": "1", "price": "1.001"}]},
        {
            "lines": [
                {"service": "x", "unit": "сутки", "quantity": "99999", "price": "9999999999.99"}
            ]
            * 20
        },
    ],
)
def test_form_rejects_empty_multiline_and_out_of_range(change: dict[str, object]) -> None:
    base: dict[str, object] = {
        "number": "15",
        "composed_on": "2026-11-11",
        "lines": [{"service": "x", "unit": "сутки", "quantity": "1", "price": "1"}],
    }

    with pytest.raises(ValidationError):
        WorkCompletionActCreate.model_validate(base | change)
