"""Очередь по службам в кабинете (spec 0045 §3–§5, §10 «PR 2»; #221 п. 2).

Сотрудник службы видит в очереди, «закрытых за сегодня», на главной и в
сигнале только свои службы; ресепшен и менеджер — всё. Чужая служба в
`?category=` — страница «Нет доступа», действие над её заявкой — 403
`ERR-REQUESTS-007`. Новую заявку руками можно в любую службу. Сигнал ресепшена
и менеджера — по лестнице: третья ступень, срочная, эскалация, служба без
сотрудников.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import queue, scope, signal
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_queue import SAME_ORIGIN, _make_request, _post_action
from tests.test_staff_auth import create_staff_user, unique_login

QUEUE = f"/staff/{HOTEL_SLUG}/requests"
HOME = f"/staff/{HOTEL_SLUG}"
SIGNAL = f"/staff/{HOTEL_SLUG}/api/requests/signal"


async def _towels(
    tenant_id: uuid.UUID, *, is_urgent: bool = False
) -> requests_api.ServiceRequestRead:
    return await _make_request(tenant_id, "TOWELS-305", is_urgent=is_urgent)


async def _pipe(tenant_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    return await _make_request(
        tenant_id,
        "PIPE-101",
        category_key="maintenance",
        category_name="Техника",
        room_number="101",
    )


async def _staff(
    client: AsyncClient, portal_hotel: PortalHotel, *service_keys: str, login: bool = True
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        service_keys=service_keys,
    )
    if login:
        await submit_login(client, staff_login)


async def _signal_ids(client: AsyncClient) -> dict[str, dict[str, object]]:
    return {item["id"]: item for item in (await client.get(SIGNAL)).json()["unclaimed"]}


# ---------------------------------------------------------------------------
# Кто что видит
# ---------------------------------------------------------------------------


async def test_staff_sees_only_own_service_everywhere(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Очередь, «закрытые за сегодня», главная и сигнал — без заявок техники."""
    towels = await _towels(portal_hotel.tenant_id)
    pipe = await _pipe(portal_hotel.tenant_id)
    closed_towels = await _make_request(portal_hotel.tenant_id, "CLOSED-TOWELS")
    closed_pipe = await _make_request(
        portal_hotel.tenant_id, "CLOSED-PIPE", category_key="maintenance"
    )
    with tenant_context(portal_hotel.tenant_id):
        for request in (closed_towels, closed_pipe):
            await requests_api.change_request_status(
                request.id, requests_api.RequestStatus.CANCELLED
            )
    await _staff(client, portal_hotel, "housekeeping")

    open_page = (await client.get(QUEUE)).text
    closed_page = (await client.get(f"{QUEUE}?tab=closed")).text
    home = (await client.get(HOME)).text
    unclaimed = await _signal_ids(client)

    assert "TOWELS-305" in open_page and "PIPE-101" not in open_page
    assert "CLOSED-TOWELS" in closed_page and "CLOSED-PIPE" not in closed_page
    assert "TOWELS-305" in home and "PIPE-101" not in home
    assert set(unclaimed) == {str(towels.id)}
    assert str(pipe.id) not in unclaimed


@pytest.mark.parametrize("who", ["reception", "manager"])
async def test_reception_and_manager_see_every_service(
    client: AsyncClient, portal_hotel: PortalHotel, who: str
) -> None:
    await _towels(portal_hotel.tenant_id)
    await _pipe(portal_hotel.tenant_id)
    if who == "reception":
        await _staff(client, portal_hotel, RECEPTION_SERVICE_KEY)
    else:
        await submit_login(client, portal_hotel.login)

    page = (await client.get(QUEUE)).text
    filtered = await client.get(f"{QUEUE}?category=maintenance")

    assert "TOWELS-305" in page and "PIPE-101" in page
    assert filtered.status_code == 200
    assert "PIPE-101" in filtered.text and "TOWELS-305" not in filtered.text


async def test_foreign_category_filter_is_forbidden(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """`?category=` чужой службы — «Нет доступа» с названием своих служб;
    поллинг того же фильтра — 403 без списка; своя служба открывается."""
    await _towels(portal_hotel.tenant_id)
    await _pipe(portal_hotel.tenant_id)
    await _staff(client, portal_hotel, "housekeeping")

    page = await client.get(f"{QUEUE}?category=maintenance")
    fragment = await client.get(f"{QUEUE}/fragment?category=maintenance")
    own = await client.get(f"{QUEUE}?category=housekeeping")

    assert page.status_code == 403
    assert "Это заявки другой службы. Вам видны заявки ваших служб: Уборка." in page.text
    assert "PIPE-101" not in page.text
    assert fragment.status_code == 403
    assert "PIPE-101" not in fragment.text
    assert own.status_code == 200
    assert "TOWELS-305" in own.text


async def test_service_chips_show_only_own_services(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _towels(portal_hotel.tenant_id)
    await _pipe(portal_hotel.tenant_id)
    await _make_request(portal_hotel.tenant_id, "WIFI", category_key="it", category_name="Связь")
    await _staff(client, portal_hotel, "housekeeping", "it")

    page = (await client.get(QUEUE)).text

    assert "?category=housekeeping" in page and "?category=it" in page
    assert "?category=maintenance" not in page


async def test_staff_without_services_sees_what_to_do(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """После миграции 0033 у бывшего `staff` служб нет: заявок не видно, текст
    говорит, у кого просить службу, сигнал молчит."""
    await _towels(portal_hotel.tenant_id)
    await _staff(client, portal_hotel)

    page = (await client.get(QUEUE)).text
    home = (await client.get(HOME)).text

    assert scope.NO_SERVICES_TEXT in page
    assert scope.NO_SERVICES_TEXT in home
    assert "TOWELS-305" not in page
    assert await _signal_ids(client) == {}


async def test_long_feed_says_it_is_cut(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Упёрлись в потолок ленты — лента говорит об этом, а не молчит (#221)."""
    monkeypatch.setattr(queue, "_QUEUE_LIMIT", 1)
    await _towels(portal_hotel.tenant_id)
    await _make_request(portal_hotel.tenant_id, "SECOND")
    await submit_login(client, portal_hotel.login)

    page = (await client.get(QUEUE)).text

    assert "Показаны 1 самых новых заявок" in page


# ---------------------------------------------------------------------------
# Запрет на сервере
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "payload", "claimed_first"),
    [
        ("claim", None, False),
        ("complete", None, True),
        ("cancel", {"note": "не наша"}, False),
        ("requeue", None, True),
        ("transfer", "housekeeping", False),
    ],
)
async def test_actions_on_foreign_service_are_403(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    action: str,
    payload: dict[str, str] | str | None,
    claimed_first: bool,
) -> None:
    towels = await _towels(portal_hotel.tenant_id)
    pipe = await _pipe(portal_hotel.tenant_id)
    with tenant_context(portal_hotel.tenant_id):
        if claimed_first:
            await requests_api.change_request_status(
                pipe.id, requests_api.RequestStatus.IN_PROGRESS
            )
        before = await requests_api.get_request(pipe.id)
    body = {"category_id": str(towels.category_id)} if payload == "housekeeping" else payload
    await _staff(client, portal_hotel, "housekeeping")

    response = await _post_action(client, pipe.id, action, body)  # type: ignore[arg-type]

    assert response.status_code == 403
    assert response.json()["error"]["code"] == requests_api.ERR_REQUESTS_FOREIGN_SERVICE
    with tenant_context(portal_hotel.tenant_id):
        assert await requests_api.get_request(pipe.id) == before


async def test_manual_request_goes_to_any_service_and_flash_names_it(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Горничную ловят в коридоре с «не работает кондиционер» — заявка уходит в
    технику; в её очереди её не будет, и плашка это говорит (§3)."""
    towels = await _towels(portal_hotel.tenant_id)
    pipe = await _pipe(portal_hotel.tenant_id)
    await _staff(client, portal_hotel, "housekeeping")

    created = await client.post(
        f"/staff/{HOTEL_SLUG}/api/requests",
        json={"room_number": "707", "category_id": str(pipe.category_id), "summary": "кондиционер"},
        headers=SAME_ORIGIN,
    )
    number = created.json()["daily_number"]
    foreign = await client.get(f"{QUEUE}?created={number}&service={pipe.category_id}")
    own = await client.get(f"{QUEUE}?created={number}&service={towels.category_id}")

    assert created.status_code == 200
    assert (
        f"Заявка #{number} создана и ушла в службу «Техника» — в вашей очереди её не будет."
        in foreign.text
    )
    assert f"Заявка #{number} создана — уведомление ушло в чат службы." in own.text


# ---------------------------------------------------------------------------
# Сигнал по лестнице
# ---------------------------------------------------------------------------


def _later(monkeypatch: pytest.MonkeyPatch, minutes: int) -> None:
    later = utc_now() + timedelta(minutes=minutes)
    monkeypatch.setattr(signal, "utc_now", lambda: later)
    monkeypatch.setattr(queue, "utc_now", lambda: later)


async def test_manager_hears_staffed_service_only_from_step_three(
    client: AsyncClient,
    second_client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Срок 30 мин: служба слышит с первой минуты, менеджер — с 60-й, и тогда
    «Никто не взял» (`is_raised`); в очереди менеджер видит заявку всегда."""
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    towels = await _towels(portal_hotel.tenant_id)
    await _staff(second_client, portal_hotel, "housekeeping")
    await submit_login(client, portal_hotel.login)

    manager_now = await _signal_ids(client)
    maid_now = await _signal_ids(second_client)
    _later(monkeypatch, 59)
    manager_overdue = await _signal_ids(client)
    _later(monkeypatch, 60)
    manager_raised = await _signal_ids(client)
    maid_raised = await _signal_ids(second_client)

    assert manager_now == {}
    assert "TOWELS-305" in (await client.get(QUEUE)).text
    assert maid_now[str(towels.id)]["is_raised"] is False
    assert manager_overdue == {}
    assert manager_raised[str(towels.id)]["is_raised"] is True
    assert maid_raised[str(towels.id)]["is_raised"] is False


async def test_urgent_and_escalation_reach_manager_at_once(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    await _staff(client, portal_hotel, "housekeeping", login=False)
    urgent = await _towels(portal_hotel.tenant_id, is_urgent=True)
    ordinary = await _towels(portal_hotel.tenant_id)
    with tenant_context(portal_hotel.tenant_id):
        escalation = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=ordinary.category_id,
                origin=requests_api.ServiceRequestOrigin.ESCALATION,
                summary="гость просит сотрудника",
                room_number="305",
            )
        )
    await submit_login(client, portal_hotel.login)

    heard = await _signal_ids(client)

    assert set(heard) == {str(urgent.id), str(escalation.id)}
    assert heard[str(urgent.id)]["is_raised"] is False


async def test_reception_signal_narrows_when_service_gets_staff(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Пока в уборке никого нет, её заявку ресепшен слышит сразу (как на этапе 3
    без служб в кабинете); появилась горничная — слышит она, ресепшен ждёт."""
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    towels = await _towels(portal_hotel.tenant_id)
    await _staff(client, portal_hotel, RECEPTION_SERVICE_KEY)

    before = await _signal_ids(client)
    await _staff(client, portal_hotel, "housekeeping", login=False)
    after = await _signal_ids(client)

    assert str(towels.id) in before
    assert after == {}


async def test_reception_hears_its_own_service_from_step_one(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    call = await _make_request(
        portal_hotel.tenant_id,
        "позвонить",
        category_key=RECEPTION_SERVICE_KEY,
        category_name="Ресепшен",
    )
    await _staff(client, portal_hotel, RECEPTION_SERVICE_KEY)

    assert str(call.id) in await _signal_ids(client)
