"""Вкладка «Отправленные» страницы «Заявки» (spec 0063 §4–§5, §7, #700).

Заявка, созданная формой сотрудником уборки в технику, видна коллегам по уборке
во вкладке и не видна технике там же (у техники она в очереди). Вкладка — только
просмотр, у менеджера и сотрудника без служб её нет, фильтры очереди она не
применяет, поиск — как у очереди.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.staff_portal.tests.test_queue import SAME_ORIGIN, _post_action
from hospitality.staff_portal.tests.test_service_queues import _staff
from tests.test_staff_auth import create_staff_user, unique_login

QUEUE = f"/staff/{HOTEL_SLUG}/requests"
SENT = f"{QUEUE}?tab=sent"
CREATE_PATH = f"/staff/{HOTEL_SLUG}/api/requests"
HINT = "Заявки, которые ваша служба отправила другим службам"
EMPTY = "Ваша служба пока не отправляла заявок другим службам."


async def _categories(tenant_id: uuid.UUID) -> dict[str, requests_api.RequestCategoryRead]:
    with tenant_context(tenant_id):
        for key, name in (("housekeeping", "Уборка"), ("maintenance", "Техника")):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
        return {category.key: category for category in await requests_api.list_categories()}


async def _send(
    client: AsyncClient, category: requests_api.RequestCategoryRead, summary: str, room: str
) -> requests_api.ServiceRequestRead:
    """Заявка формой «Новая заявка» от вошедшего сотрудника."""
    response = await client.post(
        CREATE_PATH,
        json={"category_id": str(category.id), "summary": summary, "room_number": room},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 200, response.text
    return requests_api.ServiceRequestRead.model_validate(response.json())


async def _maid_sent_to_maintenance(
    client: AsyncClient, portal_hotel: PortalHotel, *rooms: str
) -> list[requests_api.ServiceRequestRead]:
    """Горничная отправила технике по заявке на комнату; входит её коллега по уборке."""
    categories = await _categories(portal_hotel.tenant_id)
    await _staff(client, portal_hotel, "housekeeping")
    sent = [
        await _send(client, categories["maintenance"], f"течёт кран {room}", room) for room in rooms
    ]
    await _staff(client, portal_hotel, "housekeeping")
    return sent


async def test_sent_tab_shows_what_own_service_sent(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Коллега по уборке видит заявку во вкладке, а не в очереди; технике она —
    в очереди, а во «Отправленных» у техники пусто."""
    await _maid_sent_to_maintenance(client, portal_hotel, "704")

    sent_page = (await client.get(SENT)).text
    queue_page = (await client.get(QUEUE)).text
    await _staff(client, portal_hotel, "maintenance")
    engineer_sent = (await client.get(SENT)).text
    engineer_queue = (await client.get(QUEUE)).text

    assert "течёт кран 704" in sent_page
    assert 'aria-current="page">Отправленные</a>' in sent_page
    assert HINT in sent_page
    assert 'aria-label="Фильтры"' not in sent_page  # чипсов на вкладке нет
    assert "течёт кран 704" not in queue_page and HINT not in queue_page
    assert "течёт кран 704" not in engineer_sent and EMPTY in engineer_sent
    assert "течёт кран 704" in engineer_queue


async def test_sent_tab_is_read_only(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Ни кнопок, ни форм во фрагменте; «Взять» чужую заявку — по-прежнему 403."""
    (request,) = await _maid_sent_to_maintenance(client, portal_hotel, "704")

    fragment = (await client.get(f"{QUEUE}/fragment?tab=sent")).text
    claim = await _post_action(client, request.id, "claim")

    assert "течёт кран 704" in fragment
    assert "data-action" not in fragment and "<form" not in fragment
    assert claim.status_code == 403
    assert claim.json()["error"]["code"] == requests_api.ERR_REQUESTS_FOREIGN_SERVICE


@pytest.mark.parametrize("who", ["manager", "no_services"])
async def test_no_sent_tab_without_services(
    client: AsyncClient, portal_hotel: PortalHotel, who: str
) -> None:
    """Менеджеру и сотруднику без служб вкладки нет; ссылка на неё — «Открытые»."""
    if who == "manager":
        await submit_login(client, portal_hotel.login)
    else:
        staff_login = unique_login()
        await create_staff_user(staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF)
        await submit_login(client, staff_login)

    page = await client.get(SENT)

    assert page.status_code == 200
    assert "Отправленные" not in page.text and HINT not in page.text
    assert 'aria-current="page">Открытые</a>' in page.text


async def test_sent_tab_ignores_queue_filters_and_searches(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """`category`/`mine` из ссылок вкладок ленту не режут (иначе она пуста);
    поиск по комнате и `#N` — в SQL, тексты пустого поиска — свои, без «Искать за 7 дней»."""
    room_704, room_512 = await _maid_sent_to_maintenance(client, portal_hotel, "704", "512")

    filtered = (await client.get(f"{SENT}&category=housekeeping&mine=1")).text
    by_room = (await client.get(f"{SENT}&q=704")).text
    by_number = (await client.get(f"{SENT}&q=%23{room_512.daily_number}")).text
    no_room = (await client.get(f"{SENT}&q=999")).text
    no_number = (await client.get(f"{SENT}&q=%2399999")).text

    assert "течёт кран 704" in filtered and "течёт кран 512" in filtered
    assert "течёт кран 704" in by_room and "течёт кран 512" not in by_room
    assert "течёт кран 512" in by_number and "течёт кран 704" not in by_number
    assert room_704.daily_number != room_512.daily_number
    assert "Среди отправленных заявок по комнате 999 нет." in no_room
    assert "Среди отправленных заявки #99999 нет." in no_number
    assert "Искать за 7 дней" not in no_room
