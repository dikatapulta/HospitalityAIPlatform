"""Черновик АВР в кабинете (spec 0086 §3–§5, PR 2 серии; issue #953).

«Сформировать АВР» — только у строки «Проверено» и только при флаге контура
`ENABLE_WORK_COMPLETION_ACTS`; первая строка формы — проживание из запроса.
Акт создаёт JSON-действие с `Origin` (иначе ERR-AUTH-009) по праву страницы
(служба без `accounting` — 403); не «Проверено» — 007, нет исполнителя — 008.
Под строкой — «АВР № … от … · итого · кто», PDF и Excel вложением без кэша; чужой
акт — 004. Флаг выключен — ни кнопки, ни маршрутов.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.tests.conftest import make_acting_user
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from hospitality.staff_portal.tests.test_act_requests import BIN, _hotel, _login, _submit
from hospitality.staff_portal.work_completion_acts import stay_period
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_accounting_port_acts import pdf_fragments, read_cells

PAGE = f"/staff/{HOTEL_SLUG}/act-requests"
API = f"/staff/{HOTEL_SLUG}/api"
SAME_ORIGIN = {"origin": "https://test"}
NBSP = " "
EXECUTOR = {
    "name": "ТОО «Отель Тест»",
    "bin_iin": "123456789012",
    "address": "г. Алматы, пр. Абая 1",
    "contact": "+7 727 000 00 00",
}
FORM = {
    "number": "15",
    "composed_on": "2026-11-11",
    "contract": "№ 12 от 01.11.2026",
    "lines": [
        {"service": "Проживание, номер 512", "unit": "сутки", "quantity": "2", "price": "22500"}
    ],
}


@pytest.fixture(autouse=True)
def _acts_on() -> Iterator[None]:
    """Раздел включён (Р-9). Свой MonkeyPatch — довод `_fake_rate_limit` в conftest."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_WORK_COMPLETION_ACTS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _configure(tenant_id: uuid.UUID, *, executor: dict[str, str] | None = EXECUTOR) -> None:
    config = TenantConfig.model_validate(
        {
            "profile": HotelProfile(city="Almaty", country_code="KZ"),
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "act_executor": executor,
        }
    )
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, config)


async def _checked(tenant_id: uuid.UUID, *, room: str = "512") -> documents_api.ActRequestRead:
    stay_id = await _hotel(tenant_id, room)
    read = await _submit(tenant_id, stay_id, bill_to="ТОО Арман", note=f"БИН {BIN}")
    accountant = await make_acting_user("Айгуль")
    with tenant_context(tenant_id):
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)
    return read


async def _create(client: AsyncClient, act_request_id: uuid.UUID) -> httpx.Response:
    return await client.post(
        f"{API}/act-requests/{act_request_id}/acts", json=FORM, headers=SAME_ORIGIN
    )


async def test_checked_row_offers_the_form_with_the_stay_line(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _configure(portal_hotel.tenant_id)
    await _checked(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    page = (await client.get(PAGE)).text

    assert page.count("Сформировать АВР") == 1
    assert "АВР для ТОО Арман · номер 512" in page
    assert 'value="Проживание, номер 512, ' in page
    assert 'name="unit" maxlength="20" value="сутки"' in page
    assert 'value="2" autocomplete' in page  # две ночи — заезд сегодня, выезд через двое суток
    assert "Услуги и суммы вписываете вы — из PMS ничего не берётся." in page
    assert "work_completion_acts.js" in page


async def test_unchecked_row_has_no_button_and_its_act_is_refused(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _configure(portal_hotel.tenant_id)
    stay_id = await _hotel(portal_hotel.tenant_id)
    read = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман")
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    page = (await client.get(PAGE)).text
    refused = await _create(client, read.id)

    assert "Сформировать АВР" not in page
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-DOCUMENTS-007"


async def test_accountant_creates_act_and_downloads_pdf_and_excel(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
) -> None:
    await _configure(portal_hotel.tenant_id)
    read = await _checked(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    created = await _create(client, read.id)
    page = (await client.get(PAGE)).text
    act_id = created.json()["id"]
    served = await client.get(f"{PAGE}/acts/{act_id}.xlsx")
    pdf = await client.get(f"{PAGE}/acts/{act_id}.pdf")

    assert created.status_code == 200
    assert f"АВР № 15 от 11.11.2026 · 45{NBSP}000,00{NBSP}₸ · Айгуль" in page
    assert (
        f'· <a href="{PAGE}/acts/{act_id}.pdf">PDF</a>'
        f' · <a href="{PAGE}/acts/{act_id}.xlsx">Excel</a>'
    ) in page
    assert served.status_code == 200
    assert served.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert served.headers["content-disposition"].startswith("attachment")
    assert served.headers["cache-control"] == "no-store"
    cells = read_cells(served.content)
    texts = " | ".join(value for value in cells.values() if isinstance(value, str))
    assert BIN in texts and "ТОО «Отель Тест»" in texts and "№ 12 от 01.11.2026" in texts
    assert Decimal("45000") in cells.values()
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("attachment")
    assert pdf.headers["cache-control"] == "no-store"
    fragments = pdf_fragments(pdf.content)
    assert BIN in fragments and "45 000,00" in fragments
    assert "Исполнитель: ТОО «Отель Тест», г. Алматы, пр. Абая 1, +7 727 000 00 00" in fragments

    await _login(second_client, portal_hotel, StaffRole.MANAGER, ())
    journal = (await second_client.get(f"/staff/{HOTEL_SLUG}/changes")).text
    assert "АВР сформирован" in journal and "#1 · Бухгалтерия · 512" in journal


async def test_hotel_without_executor_gets_008(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _configure(portal_hotel.tenant_id, executor=None)
    read = await _checked(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    refused = await _create(client, read.id)

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "ERR-DOCUMENTS-008"


async def test_service_without_accounting_and_request_without_origin_are_refused(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
) -> None:
    await _configure(portal_hotel.tenant_id)
    read = await _checked(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())
    act_id = (await _create(client, read.id)).json()["id"]
    await _login(second_client, portal_hotel, StaffRole.STAFF, ("maintenance",))

    no_origin = await client.post(f"{API}/act-requests/{read.id}/acts", json=FORM)
    denied = await second_client.post(
        f"{API}/act-requests/{read.id}/acts", json=FORM, headers=SAME_ORIGIN
    )
    denied_file = await second_client.get(f"{PAGE}/acts/{act_id}.xlsx")

    assert no_origin.status_code == 403
    assert no_origin.json()["error"]["code"] == "ERR-AUTH-009"
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ERR-AUTH-003"
    assert denied_file.status_code == 403


async def test_other_hotel_act_is_not_served(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    """P-4: акт чужого отеля по прямой ссылке — 404 ERR-DOCUMENTS-004."""
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other Hotel")
        session.add(other)
        await session.flush()
        other_id = other.id
    await _configure(other_id)
    foreign = await _checked(other_id, room="101")
    with tenant_context(other_id):
        act = await documents_api.create_work_completion_act(
            foreign.id,
            documents_api.WorkCompletionActCreate.model_validate(FORM),
            acting_user=await make_acting_user("Чужой"),
        )
    await _configure(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    served = await client.get(f"{PAGE}/acts/{act.id}.xlsx")
    served_pdf = await client.get(f"{PAGE}/acts/{act.id}.pdf")
    created = await _create(client, foreign.id)

    assert served.status_code == 404
    assert served.json()["error"]["code"] == "ERR-DOCUMENTS-004"
    assert served_pdf.status_code == 404
    assert created.status_code == 404


async def test_switch_off_hides_button_list_and_routes(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Р-9: без флага контура — ни кнопки, ни списка, маршруты актов 404."""
    await _configure(portal_hotel.tenant_id)
    read = await _checked(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())
    act_id = (await _create(client, read.id)).json()["id"]
    monkeypatch.setenv("ENABLE_WORK_COMPLETION_ACTS", "false")
    get_settings.cache_clear()

    page = (await client.get(PAGE)).text
    created = await _create(client, read.id)
    served = await client.get(f"{PAGE}/acts/{act_id}.xlsx")

    assert "Проверено" in page
    assert "Сформировать АВР" not in page and "АВР № 15" not in page
    assert [created.status_code, served.status_code] == [404, 404]


def test_stay_period_carries_the_year_once_or_twice() -> None:
    assert stay_period(date(2026, 11, 10), date(2026, 11, 12)) == "10.11–12.11.2026"
    assert stay_period(date(2026, 12, 30), date(2027, 1, 2)) == "30.12.2026–02.01.2027"
