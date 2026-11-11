"""Поля из недоверенного ввода гостя на выходе: страница «Документы для АВР» и файл
для 1С (issue #954, FOUNDATION §7.6).

Свободные поля реквизитов — название, банк, адрес — хранят текст как он написан
гостем, в его файле или прочитан моделью с фото: это данные. Обезвреживает их
показ: страница экранирует разметку, а файл для 1С пишет значение строкой, не
формулой. Полезная нагрузка — из набора атак (`documents/tests/attacks.py`).
"""

# ruff: noqa: F811

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import timedelta
from xml.etree import ElementTree

from httpx import AsyncClient

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.tests.attacks import FORMULA, HTML, SCRIPT
from hospitality.modules.documents.tests.conftest import make_requisites_docx
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY
from hospitality.shared.db import utc_now
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from hospitality.staff_portal.tests.test_act_requests import _hotel, _login
from hospitality.staff_portal.tests.test_act_requests_export import _check
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_accounting_port import read_xlsx

PAGE = f"/staff/{HOTEL_SLUG}/act-requests"
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# Имя файла гостя — тоже его текст: показывается ссылкой на странице.
_FILE_NAME = '"><img src=x onerror=alert(2)>.docx'


async def _hostile_request(portal_hotel: PortalHotel) -> documents_api.ActRequestRead:
    stay_id = await _hotel(portal_hotel.tenant_id)
    with tenant_context(portal_hotel.tenant_id):
        read = await documents_api.submit_act_request(
            documents_api.ActRequestSubmit(
                stay_id=stay_id,
                guest_identity_id=uuid.uuid4(),
                client_key="untrusted-input",
                language="ru",
                bill_to=HTML,
                note=f"Банк: {FORMULA}\nАдрес: {SCRIPT}",
            ),
            [
                documents_api.UploadInput(
                    filename=_FILE_NAME, data=make_requisites_docx([("x", "y")])
                )
            ],
        )
        await documents_api.extract_requisites(read.id)
    return read


async def test_page_shows_markup_from_the_guest_as_text(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    await _hostile_request(portal_hotel)
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    page = (await client.get(PAGE)).text

    # Значения дошли до страницы — и ни одно не стало разметкой.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'data-copy="&lt;img src=x onerror=alert(1)&gt;"' in page
    assert "=HYPERLINK(&#34;http://evil.example&#34;,&#34;Halyk&#34;)" in page
    assert "<script>alert" not in page
    assert "<img src=x" not in page


async def test_export_for_1c_writes_formula_and_markup_as_strings(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    read = await _hostile_request(portal_hotel)
    await _check(portal_hotel.tenant_id, read)
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))
    today = utc_now().date()
    period = {"from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))}

    response = await client.get(f"{PAGE}/export", params=period)

    assert response.status_code == 200, response.text
    (row,) = read_xlsx(response.content)[1:]
    assert (row[0], row[2], row[5]) == (HTML, SCRIPT, FORMULA)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    cells = list(sheet.iter(f"{_SHEET_NS}c"))
    assert cells and all(cell.get("t") == "s" for cell in cells)
    assert not list(sheet.iter(f"{_SHEET_NS}f"))
