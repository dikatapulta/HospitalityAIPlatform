"""Страница «Документы для АВР» в кабинете (spec 0049 §5–§7, PR 3 серии; issue #493).

Права: служба «Бухгалтерия», ресепшен и менеджер видят страницу, правят и
жмут «Проверено»; служба без `accounting` — 403 и пункта меню нет. Пункт меню —
только при `ENABLE_GUEST_DOCUMENTS`. Правка снимает «Проверено» и пишет журнал
без значений; файл чужого запроса или чужого отеля — 004; JSON без Origin —
ERR-AUTH-009; брошенный запрос (без заявки) не показывается; карточка очереди
ведёт на страницу.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.documents import api as documents_api
from hospitality.modules.documents.tests.conftest import make_requisites_docx
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_services import ACCOUNTING_SERVICE_KEY, RECEPTION_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_staff_auth import create_staff_user, unique_login

PAGE = f"/staff/{HOTEL_SLUG}/act-requests"
API = f"/staff/{HOTEL_SLUG}/api"
SAME_ORIGIN = {"origin": "https://test"}
BIN = "940140000385"
NEW_BIN = "971240001315"


@pytest.fixture(autouse=True)
def _guest_documents_on() -> Iterator[None]:
    """Приём реквизитов включён (Р-7): пункт меню виден. Свой MonkeyPatch — довод
    `_fake_rate_limit` в conftest."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_GUEST_DOCUMENTS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


async def _hotel(tenant_id: uuid.UUID, room: str = "512") -> uuid.UUID:
    """Службы ресепшена, бухгалтерии и инженерии; заселённый гость. Вернуть Stay."""
    with tenant_context(tenant_id):
        for key, name in (
            (RECEPTION_SERVICE_KEY, "Ресепшен"),
            (ACCOUNTING_SERVICE_KEY, "Бухгалтерия"),
            ("maintenance", "Инженерная служба"),
        ):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
        checked_in = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + timedelta(days=2))
        )
    return checked_in.stay.id


async def _submit(
    tenant_id: uuid.UUID, stay_id: uuid.UUID, *, language: str = "ru", **form: str
) -> documents_api.ActRequestRead:
    with tenant_context(tenant_id):
        read = await documents_api.submit_act_request(
            documents_api.ActRequestSubmit(
                stay_id=stay_id,
                guest_identity_id=uuid.uuid4(),
                client_key=uuid.uuid4().hex,
                language=language,  # type: ignore[arg-type]
                bill_to=form.get("bill_to", ""),
                note=form.get("note", ""),
            ),
            [
                documents_api.UploadInput(
                    filename="rekv.docx", data=make_requisites_docx([("БИН", BIN)])
                )
            ]
            if form.get("with_file")
            else [],
        )
        await documents_api.extract_requisites(read.id)
    return read


async def _login(
    client: AsyncClient, portal_hotel: PortalHotel, role: StaffRole, services: tuple[str, ...]
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=role,
        service_keys=services,
        display_name="Айгуль",
    )
    await submit_login(client, staff_login)


async def _post(client: AsyncClient, path: str, body: dict[str, str]) -> httpx.Response:
    return await client.post(f"{API}{path}", json=body, headers=SAME_ORIGIN)


@pytest.mark.parametrize(
    ("role", "services", "sees"),
    [
        (StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,), True),
        (StaffRole.SENIOR, (ACCOUNTING_SERVICE_KEY,), True),
        (StaffRole.STAFF, (RECEPTION_SERVICE_KEY,), True),
        (StaffRole.MANAGER, (), True),
        (StaffRole.STAFF, ("maintenance",), False),
        (StaffRole.SENIOR, ("maintenance",), False),
    ],
)
async def test_page_menu_and_actions_by_service(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
    role: StaffRole,
    services: tuple[str, ...],
    sees: bool,
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    read = await _submit(portal_hotel.tenant_id, stay_id, note=f"БИН {BIN}")
    await _login(client, portal_hotel, role, services)

    home = await client.get(f"/staff/{HOTEL_SLUG}")
    page = await client.get(PAGE)
    checked = await _post(client, f"/act-requests/{read.id}/checked", {})

    assert ("Документы для АВР" in home.text) is sees
    assert page.status_code == (200 if sees else 403)
    assert (BIN in page.text) is sees
    assert checked.status_code == (200 if sees else 403)
    if not sees:
        assert checked.json()["error"]["code"] == "ERR-AUTH-003"


async def test_menu_needs_the_switch_but_the_page_does_not(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Р-7: без выключателя пункта нет; прошлые запросы — по ссылке до их срока."""
    monkeypatch.setenv("ENABLE_GUEST_DOCUMENTS", "false")
    get_settings.cache_clear()
    await _hotel(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    home = await client.get(f"/staff/{HOTEL_SLUG}")
    page = await client.get(PAGE)

    assert "Документы для АВР" not in home.text
    assert page.status_code == 200
    assert "Запросов пока нет. Гости присылают реквизиты скрепкой в чате." in page.text


async def test_row_shows_fields_file_guest_text_and_language(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    await _submit(
        portal_hotel.tenant_id,
        stay_id,
        language="en",
        bill_to="ТОО Арман",
        note="до пятницы",
        with_file="1",
    )
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    page = (await client.get(PAGE)).text

    assert BIN in page and "ТОО Арман" in page
    assert f'data-copy="{BIN}" data-label="БИН/ИИН"' in page  # значение — в буфер для 1С
    assert "Гость написал: ТОО Арман — до пятницы" in page
    assert "Язык гостя: English — примечание он прочитает как есть." in page
    assert "1. rekv.docx" in page
    assert "Проверить" in page and "новый" in page
    assert 'data-action="claim"' in page and 'data-action="cancel"' in page


async def test_check_then_edit_unchecks_and_journal_has_no_values(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    fake_file_storage: FileStoragePort,
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    read = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман", note=f"БИН {BIN}")
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    assert (await _post(client, f"/act-requests/{read.id}/checked", {})).status_code == 200
    assert "Проверено" in (await client.get(PAGE)).text
    edited = await _post(client, f"/act-requests/{read.id}/requisites", {"bin_iin": NEW_BIN})
    wrong = await _post(client, f"/act-requests/{read.id}/requisites", {"iik": "KZ00"})
    page = (await client.get(PAGE)).text

    assert edited.status_code == 200
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "ERR-DOCUMENTS-006"
    assert NEW_BIN in page and 'data-action="checked"' in page

    await _login(second_client, portal_hotel, StaffRole.MANAGER, ())
    journal = (await second_client.get(f"/staff/{HOTEL_SLUG}/changes")).text
    assert "Реквизиты исправлены" in journal and "Реквизиты проверены" in journal
    assert "#1 · Бухгалтерия · 512" in journal
    assert NEW_BIN not in journal and BIN not in journal and "Арман" not in journal


async def test_file_is_served_as_attachment_and_only_from_its_request(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    first = await _submit(portal_hotel.tenant_id, stay_id, with_file="1")
    second = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ИП Иванов")
    with tenant_context(portal_hotel.tenant_id):
        (row_first,) = [
            row
            for row in await documents_api.list_act_requests(
                created_from=utc_now() - timedelta(days=1),
                created_before=utc_now() + timedelta(days=1),
            )
            if row.id == first.id
        ]
    file_id = row_first.files[0].file_id
    await _login(client, portal_hotel, StaffRole.STAFF, (RECEPTION_SERVICE_KEY,))

    served = await client.get(f"{PAGE}/{first.id}/files/{file_id}")
    foreign = await client.get(f"{PAGE}/{second.id}/files/{file_id}")

    assert served.status_code == 200
    assert served.headers["cache-control"] == "no-store"
    assert served.headers["content-disposition"].startswith("attachment")
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "ERR-DOCUMENTS-004"


async def test_json_action_without_origin_is_refused(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    read = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман")
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    response = await client.post(f"{API}/act-requests/{read.id}/checked", json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-AUTH-009"


async def test_other_hotel_rows_files_and_actions_are_invisible(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    """P-4: запрос чужого отеля не виден на странице, не правится и не отдаёт файл."""
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other Hotel")
        session.add(other)
        await session.flush()
        other_id = other.id
    foreign = await _submit(other_id, await _hotel(other_id, "101"), bill_to="ТОО Чужое")
    with tenant_context(other_id):
        (foreign_row,) = await documents_api.list_act_requests(
            created_from=utc_now() - timedelta(days=1), created_before=utc_now() + timedelta(days=1)
        )
    await _hotel(portal_hotel.tenant_id)
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    page = (await client.get(PAGE)).text
    edited = await _post(client, f"/act-requests/{foreign.id}/requisites", {"bin_iin": NEW_BIN})
    checked = await _post(client, f"/act-requests/{foreign.id}/checked", {})
    served = await client.get(f"{PAGE}/{foreign.id}/files/{uuid.uuid4()}")

    assert "Чужое" not in page
    assert foreign_row.files == []
    assert [edited.status_code, checked.status_code, served.status_code] == [404, 404, 404]
    assert edited.json()["error"]["code"] == "ERR-DOCUMENTS-004"


async def test_abandoned_request_is_not_shown(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    """Ревью PR #510 Н-28: строка без заявки — не запрос; «Готово» по ней не нажать."""
    stay_id = await _hotel(portal_hotel.tenant_id)

    async def crash(*_: object, **__: object) -> None:
        raise RuntimeError("process died")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(RuntimeError):
        patch.setattr(requests_api, "create_request", crash)
        await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Брошенное")
    await _login(client, portal_hotel, StaffRole.MANAGER, ())

    page = (await client.get(PAGE)).text

    assert "Брошенное" not in page
    assert "Запросов пока нет." in page


async def test_queue_card_links_to_the_page_and_claim_works_from_it(
    client: AsyncClient, portal_hotel: PortalHotel, fake_file_storage: FileStoragePort
) -> None:
    stay_id = await _hotel(portal_hotel.tenant_id)
    read = await _submit(portal_hotel.tenant_id, stay_id, bill_to="ТОО Арман")
    await _login(client, portal_hotel, StaffRole.STAFF, (ACCOUNTING_SERVICE_KEY,))

    queue = (await client.get(f"/staff/{HOTEL_SLUG}/requests")).text
    claimed = await _post(client, f"/requests/{read.request_id}/claim", {})
    page = (await client.get(PAGE)).text

    assert "📄 Реквизиты для АВР" in queue
    assert f'href="{PAGE}#request-{read.request_id}"' in queue
    assert "Открыть в «Документах для АВР»" in queue
    assert claimed.status_code == 200
    assert "в работе" in page
    assert 'data-action="complete"' in page and 'data-action="claim"' not in page
