"""Горничная видит заявки уборки своих этажей; старшая направляет (spec 0053 §2, §4а; #856).

Очередь горничной 7-го при команде на 8-м — без 805, с 704 и «Люкс»; горничная
без этажей — только «Люкс»; старшая — всё и кнопку «Направить» со списком
горничных. Направленная заявка появляется в очереди и сигнале адресата.
Направляют старший уборки и менеджер, адресат — сотрудник уборки этого отеля,
заявка — только уборки. Без срока уборки этажи не сужают и ленту.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from hospitality.staff_portal.tests.test_floors import _heard_by, _member
from hospitality.staff_portal.tests.test_queue import _make_request
from hospitality.staff_portal.tests.test_turnovers import HOUSEKEEPING, SAME_ORIGIN, _categories

QUEUE = f"/staff/{HOTEL_SLUG}/requests"


def _direct_url(request_id: uuid.UUID) -> str:
    return f"/staff/{HOTEL_SLUG}/api/requests/{request_id}/direct"


@pytest.fixture(autouse=True)
def _floors_enabled() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_HOUSEKEEPING_FLOORS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@dataclass(frozen=True)
class Team:
    ids: dict[str, uuid.UUID]
    logins: dict[str, str]
    requests: dict[str, requests_api.ServiceRequestRead]


async def _team(portal_hotel: PortalHotel, *, reminder_after_minutes: int | None = 30) -> Team:
    """Айгуль — 7-й, Дина — 8-й, у Гульнар и старшей Сауле этажей нет; заявки
    уборки из 704, 805 и «Люкс»."""
    await _categories(portal_hotel)
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=reminder_after_minutes)
    people = {
        "aigul": (StaffRole.STAFF, "Айгуль"),
        "dina": (StaffRole.STAFF, "Дина"),
        "gulnar": (StaffRole.STAFF, "Гульнар"),
        "saule": (StaffRole.SENIOR, "Сауле"),
    }
    members = {
        key: await _member(portal_hotel, role, HOUSEKEEPING, name)
        for key, (role, name) in people.items()
    }
    ids = {key: user_id for key, (user_id, _) in members.items()}
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.replace_floor_assignments(
            {ids["aigul"]: frozenset({7}), ids["dina"]: frozenset({8})}
        )
    requests = {
        room: await _make_request(portal_hotel.tenant_id, f"полотенца {room}", room_number=room)
        for room in ("704", "805", "Люкс")
    }
    return Team(ids, {key: login for key, (_, login) in members.items()}, requests)


async def _queue_rooms(client: AsyncClient, login: str) -> set[str]:
    await submit_login(client, login)
    page = (await client.get(QUEUE)).text
    return {room for room in ("704", "805", "Люкс") if f"полотенца {room}" in page}


async def test_maid_queue_shows_only_her_floor_senior_sees_all(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    team = await _team(portal_hotel)

    assert await _queue_rooms(client, team.logins["aigul"]) == {"704", "Люкс"}
    assert await _queue_rooms(client, team.logins["dina"]) == {"805", "Люкс"}
    assert await _queue_rooms(client, team.logins["gulnar"]) == {"Люкс"}
    assert await _queue_rooms(client, team.logins["saule"]) == {"704", "805", "Люкс"}
    assert await _queue_rooms(client, portal_hotel.login) == {"704", "805", "Люкс"}


async def test_floors_do_not_narrow_the_feed_without_cleaning_delay(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Срок уборки выключен — ступеней нет, и забытая раздача не прячет заявку."""
    team = await _team(portal_hotel, reminder_after_minutes=None)

    assert await _queue_rooms(client, team.logins["gulnar"]) == {"704", "805", "Люкс"}


async def test_senior_directs_request_to_maid_of_another_floor(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """805 — Гульнар: появилась в её очереди и сигнале, в панели «Направлена —
    Гульнар», в журнале — «Направлена горничной»; Айгуль 805 по-прежнему не видит."""
    team = await _team(portal_hotel)
    request = team.requests["805"]
    await submit_login(client, team.logins["saule"])

    senior_queue = (await client.get(QUEUE)).text
    response = await client.post(
        _direct_url(request.id), json={"user_id": str(team.ids["gulnar"])}, headers=SAME_ORIGIN
    )
    after = (await client.get(QUEUE)).text
    journal = (await client.get(f"/staff/{HOTEL_SLUG}/changes")).text
    heard = await _heard_by(portal_hotel, team.ids)

    assert 'data-action="direct"' in senior_queue
    assert "Айгуль — этаж 7" in senior_queue
    assert "Гульнар — без этажа" in senior_queue
    assert response.status_code == 200
    assert response.json()["directed_to_display_name"] == "Гульнар"
    assert "<dt>Направлена</dt><dd>Гульнар</dd>" in after
    assert "Направлена горничной" in journal
    assert await _queue_rooms(second_client, team.logins["gulnar"]) == {"805", "Люкс"}
    assert await _queue_rooms(second_client, team.logins["aigul"]) == {"704", "Люкс"}
    assert "805" in heard["gulnar"]
    assert "805" not in heard["aigul"]


async def test_maid_has_no_direct_button_and_cannot_direct(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    team = await _team(portal_hotel)
    await submit_login(client, team.logins["aigul"])

    queue_page = (await client.get(QUEUE)).text
    response = await client.post(
        _direct_url(team.requests["704"].id),
        json={"user_id": str(team.ids["aigul"])},
        headers=SAME_ORIGIN,
    )

    assert 'data-action="direct"' not in queue_page
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ERR-HOUSEKEEPING-003"


async def test_direct_refuses_outsider_foreign_service_and_switched_off_section(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Адресат не в уборке — 409 006; заявка техникам — 403 ERR-REQUESTS-007;
    раздел выключен — 404. Заявка в базе не меняется."""
    team = await _team(portal_hotel)
    tech, _ = await _member(portal_hotel, StaffRole.STAFF, ("maintenance",), "Ержан")
    pipe = await _make_request(
        portal_hotel.tenant_id,
        "труба 805",
        category_key="maintenance",
        category_name="Техслужба",
        room_number="805",
    )
    await submit_login(client, portal_hotel.login)

    outsider = await client.post(
        _direct_url(team.requests["805"].id), json={"user_id": str(tech)}, headers=SAME_ORIGIN
    )
    foreign = await client.post(
        _direct_url(pipe.id), json={"user_id": str(team.ids["gulnar"])}, headers=SAME_ORIGIN
    )
    monkeypatch.setenv("ENABLE_HOUSEKEEPING_FLOORS", "false")
    get_settings.cache_clear()
    switched_off = await client.post(
        _direct_url(team.requests["805"].id),
        json={"user_id": str(team.ids["gulnar"])},
        headers=SAME_ORIGIN,
    )
    with tenant_context(portal_hotel.tenant_id):
        unchanged = await requests_api.get_request(team.requests["805"].id)

    assert (outsider.status_code, outsider.json()["error"]["code"]) == (
        409,
        "ERR-HOUSEKEEPING-006",
    )
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (403, "ERR-REQUESTS-007")
    assert switched_off.status_code == 404
    assert unchanged.directed_to_user_id is None


async def test_taken_request_stays_with_the_maid_after_redistribution(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Айгуль взяла 704, раздачу поменяли — 7-й теперь у Дины: 704 у Айгуль остаётся."""
    team = await _team(portal_hotel)
    await submit_login(client, team.logins["aigul"])
    claimed = await client.post(
        f"/staff/{HOTEL_SLUG}/api/requests/{team.requests['704'].id}/claim",
        json={},
        headers=SAME_ORIGIN,
    )
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.replace_floor_assignments(
            {team.ids["aigul"]: frozenset({9}), team.ids["dina"]: frozenset({7, 8})}
        )

    assert claimed.status_code == 200
    assert await _queue_rooms(client, team.logins["aigul"]) == {"704", "Люкс"}
