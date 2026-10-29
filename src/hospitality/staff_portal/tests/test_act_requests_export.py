"""«Скачать для 1С» на странице «Документы для АВР» (spec 0052, issue #517).

Через полный стек: адаптер — настоящий Excel для 1С, его кладёт `create_app`.
Право — как у страницы; файл — вложением, без кэша, строки «Проверено» за даты
фильтра. Нет таких строк — назад на страницу с теми же датами и пояснением.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.tests.conftest import make_acting_user
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from hospitality.staff_portal.tests.test_act_requests import BIN, _hotel, _login, _submit
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_accounting_port import read_xlsx

PAGE = f"/staff/{HOTEL_SLUG}/act-requests"
EXPORT = f"{PAGE}/export"


async def _check(tenant_id: uuid.UUID, read: documents_api.ActRequestRead) -> None:
    accountant = await make_acting_user("Айгуль")
    with tenant_context(tenant_id):
        await documents_api.mark_requisites_checked(read.id, acting_user=accountant)


async def test_accountant_downloads_checked_rows_as_excel(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    checked = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман", note=f"БИН {BIN}")
    await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Непроверенный")
    await _check(portal_hotel.tenant_id, checked)
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))
    # День отеля и день UTC расходятся около полуночи — берём три дня вокруг.
    today = utc_now().date()
    period = {"from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))}

    page = await client.get(PAGE)
    response = await client.get(EXPORT, params={**period, "status": "to_check"})

    assert f'formaction="{EXPORT}"' in page.text and "Скачать для 1С" in page.text
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    table = read_xlsx(response.content)
    assert table[0][:6] == ["Контрагент", "БИН/ИИН", "Адрес", "ИИК", "БИК", "Банк"]
    assert [row[:2] + row[6:7] for row in table[1:]] == [["ТОО Арман", BIN, "512"]]


async def test_no_checked_rows_returns_to_the_page_with_a_notice(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Непроверенный")
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    response = await client.get(
        EXPORT, params={"from": "2026-10-01", "to": "2026-10-29", "status": "checked"}
    )
    location = response.headers["location"]
    page = await client.get(location)

    assert response.status_code == 303
    assert urlsplit(location).path == PAGE
    assert parse_qs(urlsplit(location).query) == {
        "from": ["2026-10-01"],
        "to": ["2026-10-29"],
        "status": ["checked"],
        "export": ["empty"],
    }
    assert "За эти даты нет строк «Проверено» — выгружать нечего." in page.text
    assert 'value="2026-10-01"' in page.text


async def test_service_without_accounting_gets_no_file(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    checked = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман", note=f"БИН {BIN}")
    await _check(portal_hotel.tenant_id, checked)
    await _login(client, portal_hotel, StaffRole.SENIOR, ("maintenance",))

    response = await client.get(EXPORT)

    assert response.status_code == 403
    assert BIN not in response.text


async def test_without_the_key_returns_to_the_page_that_says_so(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ключа шифрования нет (ERR-FILES-005) — не 500, а назад на страницу с её текстом."""
    stay_id = await _hotel(portal_hotel.tenant_id)
    checked = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман", note=f"БИН {BIN}")
    await _check(portal_hotel.tenant_id, checked)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    today = utc_now().date()

    response = await client.get(
        EXPORT,
        params={"from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))},
    )
    page = await client.get(response.headers["location"])

    assert response.status_code == 303
    assert "export" not in parse_qs(urlsplit(response.headers["location"]).query)
    assert "Реквизиты сейчас недоступны" in page.text
