"""Запрос столика в кабинете (spec 0064 §2.3, §5, §6 PR 3; issue #702).

Карточка в очереди: «🍽 Столик #N · ресторан · день, время · гости · № комнаты», имя и
пожелания, три ответа и «Передать» — без «Взять», «Готово», «Вернуть в очередь» и
«Отменить» (ревью PR #711, Н-1). Ответ закрывает заявку и называет сотрудника; «Другое
время» без часа — 002; чужая служба — 007, ресепшен — может; не бронь и чужой тенант —
004; без `Origin` — ERR-AUTH-009. Час брони прошёл без ответа — «Закрыть запрос».
Сигнал помечает бронь — уведомление «Запрос на столик #N».
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time, timedelta

import httpx
import pytest
from httpx import AsyncClient

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.modules.table_booking.tests.conftest import (  # noqa: F401 (фикстура clock)
    Clock,
    clock,
    configure_tenant,
    create,
    table_booking_config,
)
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import booking_cards
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from tests.test_staff_auth import create_staff_user, unique_login

QUEUE = f"/staff/{HOTEL_SLUG}/requests"
FRAGMENT = f"{QUEUE}/fragment"
API = f"/staff/{HOTEL_SLUG}/api"
SAME_ORIGIN = {"origin": "https://test"}
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")  # fmt: skip


@dataclass(frozen=True)
class Stand:
    tenant_id: uuid.UUID
    stay_id: uuid.UUID


async def _stand(tenant_id: uuid.UUID) -> Stand:
    """Ресепшен, ресторан (`fnb`) и уборка; бронь «Терраса» 12:00–22:00; гость в 512."""
    with tenant_context(tenant_id):
        for key, name in (
            (RECEPTION_SERVICE_KEY, "Ресепшен"),
            ("fnb", "Ресторан"),
            ("housekeeping", "Уборка"),
        ):
            await requests_api.create_category(
                requests_api.RequestCategoryCreate(key=key, name=name)
            )
        stay = await check_in(
            StayCheckIn(
                room_number="512",
                check_out_at=utc_now() + timedelta(days=2),
                guest_display_name="Wang Li",
            )
        )
    await configure_tenant(tenant_id, table_booking_config())
    return Stand(tenant_id, stay.stay.id)


@pytest.fixture
async def stand(portal_hotel: PortalHotel, clock: Clock) -> Stand:
    return await _stand(portal_hotel.tenant_id)


async def _book(
    stand: Stand, clock: Clock, *, at: time = time(19, 0)
) -> table_booking_api.BookingRead:
    data = create(clock, at=at, key=uuid.uuid4().hex, guest_name="Айгерим", wishes="столик у окна")
    with tenant_context(stand.tenant_id):
        placed = await table_booking_api.request_booking(stand.stay_id, uuid.uuid4(), data)
    return placed.booking


async def _login(
    client: AsyncClient, portal_hotel: PortalHotel, services: tuple[str, ...], name: str
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name=name,
        service_keys=services,
    )
    await submit_login(client, staff_login)


async def _answer(client: AsyncClient, request_id: uuid.UUID, body: object) -> httpx.Response:
    return await client.post(f"{API}/bookings/{request_id}/answer", json=body, headers=SAME_ORIGIN)


def _card(page: str, request_id: uuid.UUID) -> str:
    return page.split(f'data-request-id="{request_id}"')[1].split("</article>")[0]


async def test_card_confirms_and_names_who_answered(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand, clock: Clock
) -> None:
    booking = await _book(stand, clock)
    rid = booking.request_id
    await _login(client, portal_hotel, ("fnb",), "Айгерим")
    day = f"{clock.today().day} {MONTHS[clock.today().month - 1]}"

    card = _card((await client.get(QUEUE)).text, rid)
    confirmed = await _answer(client, rid, {"answer": "confirmed"})
    closed = (await client.get(f"{FRAGMENT}?tab=closed")).text
    with tenant_context(stand.tenant_id):
        request = await requests_api.get_request(rid)

    assert f"🍽 Столик #1 · Терраса · {day}, 19:00 · 4 гост. · № 512" in card
    assert "На имя: Айгерим" in card and "Пожелания: столик у окна" in card
    assert 'data-action="booking-confirm">Подтвердить<' in card
    assert 'data-action="booking-other-time">Предложить другое время<' in card
    assert 'data-action="booking-no-seats">Мест нет<' in card
    assert 'data-action="transfer">Передать<' in card  # ошиблись службой
    assert '<option value="20:30">20:30</option>' in card
    assert '<option value="19:00">' not in card  # запрошенное время — не «другое»
    assert booking_cards.ANSWER_HINT in card and ">запрос<" in card and ">Ответить<" in card
    for generic in ("claim", "complete", "requeue", "cancel"):
        assert f'data-action="{generic}"' not in card  # ответ сам берёт и закрывает
    assert confirmed.status_code == 200 and confirmed.json()["stage"] == "confirmed"
    assert request.status is requests_api.RequestStatus.DONE
    assert request.claimed_by_display_name == "Айгерим"
    assert "Подтверждено: Айгерим, " in closed and ">подтверждён<" in closed
    assert 'data-action="booking-confirm"' not in closed


async def test_other_time_needs_a_slot_and_extra_fields_are_refused(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand, clock: Clock
) -> None:
    booking = await _book(stand, clock)
    rid = booking.request_id
    await _login(client, portal_hotel, ("fnb",), "Айгерим")

    no_time = await _answer(client, rid, {"answer": "other_time"})
    extra = await _answer(client, rid, {"answer": "no_seats", "note": "x"})
    proposed = await _answer(client, rid, {"answer": "other_time", "proposed_time": "20:30"})
    other = await _answer(client, rid, {"answer": "no_seats"})
    closed = (await client.get(f"{FRAGMENT}?tab=closed")).text

    assert no_time.status_code == 422
    assert no_time.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_INVALID
    assert extra.status_code == 422
    assert proposed.status_code == 200 and proposed.json()["proposed_time"] == "20:30"
    assert other.status_code == 409
    assert other.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE
    assert "Предложено 20:30: Айгерим, " in closed and ">другое время<" in closed


async def test_service_boundary_non_booking_and_no_origin(
    portal_hotel: PortalHotel,
    client: AsyncClient,
    second_client: AsyncClient,
    stand: Stand,
    clock: Clock,
) -> None:
    """Уборка не отвечает за ресторан (007, ответа нет); ресепшен — отвечает; заявка не
    бронь — 004; JSON без `Origin` — ERR-AUTH-009."""
    booking = await _book(stand, clock)
    with tenant_context(stand.tenant_id):
        fnb = next(c for c in await requests_api.list_categories() if c.key == "fnb")
        plain = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=fnb.id,
                origin=requests_api.ServiceRequestOrigin.API,
                summary="полотенца",
                room_number="512",
            )
        )
    await _login(client, portal_hotel, ("housekeeping",), "Ержан")
    await _login(second_client, portal_hotel, (RECEPTION_SERVICE_KEY,), "Дана")

    foreign = await _answer(client, booking.request_id, {"answer": "confirmed"})
    with tenant_context(stand.tenant_id):
        kept = await table_booking_api.get_booking_by_request(booking.request_id)
    not_booking = await _answer(second_client, plain.id, {"answer": "confirmed"})
    no_origin = await second_client.post(
        f"{API}/bookings/{booking.request_id}/answer", json={"answer": "confirmed"}
    )
    reception = await _answer(second_client, booking.request_id, {"answer": "no_seats"})

    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == requests_api.ERR_REQUESTS_FOREIGN_SERVICE
    assert kept is not None and kept.answer is None
    assert not_booking.status_code == 404
    assert not_booking.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_NOT_FOUND
    assert no_origin.status_code == 403 and no_origin.json()["error"]["code"] == "ERR-AUTH-009"
    assert reception.status_code == 200 and reception.json()["stage"] == "no_seats"


async def test_hour_passed_without_answer_is_closed_from_the_card(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand, clock: Clock
) -> None:
    booking = await _book(stand, clock, at=time(12, 0))
    rid = booking.request_id
    await _login(client, portal_hotel, ("fnb",), "Айгерим")
    early = await client.post(f"{API}/bookings/{rid}/close", json={}, headers=SAME_ORIGIN)
    clock.now = clock.local(0, time(12, 30))

    card = _card((await client.get(QUEUE)).text, rid)
    closed = await client.post(f"{API}/bookings/{rid}/close", json={}, headers=SAME_ORIGIN)
    closed_tab = (await client.get(f"{FRAGMENT}?tab=closed")).text

    assert early.status_code == 409
    assert booking_cards.LATE_HINT in card and ">не ответили<" in card
    assert 'data-action="booking-close">Закрыть запрос<' in card
    assert 'data-action="booking-confirm"' not in card
    assert closed.status_code == 200 and closed.json()["stage"] == "no_answer"
    assert "Закрыт без ответа: Айгерим, " in closed_tab
    assert 'data-action="booking-close"' not in closed_tab


async def test_open_request_is_answered_after_booking_is_switched_off(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand, clock: Clock
) -> None:
    """Ревью PR #711, Н-3: выключили бронь — карточка и ответ остаются, иначе открытый
    запрос не закрыть ничем («Готово» и «Отменить» у брони нет)."""
    booking = await _book(stand, clock)
    await configure_tenant(stand.tenant_id, table_booking_config(enabled=False))
    await _login(client, portal_hotel, ("fnb",), "Айгерим")

    card = _card((await client.get(QUEUE)).text, booking.request_id)
    answered = await _answer(client, booking.request_id, {"answer": "no_seats"})

    assert 'data-action="booking-no-seats"' in card
    assert answered.status_code == 200 and answered.json()["stage"] == "no_seats"


async def test_signal_marks_bookings(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand, clock: Clock
) -> None:
    """Сигнал: у брони `is_booking` — уведомление «Запрос на столик #N»; заявка — нет."""
    booking = await _book(stand, clock)
    with tenant_context(stand.tenant_id):
        fnb = next(c for c in await requests_api.list_categories() if c.key == "fnb")
        plain = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=fnb.id,
                origin=requests_api.ServiceRequestOrigin.API,
                summary="полотенца",
                room_number="512",
            )
        )
    await _login(client, portal_hotel, ("fnb",), "Айгерим")

    response = await client.get(f"{API}/requests/signal")

    flags = {item["id"]: item["is_booking"] for item in response.json()["unclaimed"]}
    assert flags == {str(booking.request_id): True, str(plain.id): False}


async def test_booking_of_other_tenant_is_not_found(
    portal_hotel: PortalHotel, client: AsyncClient, stand: Stand, clock: Clock
) -> None:
    """P-4: сотрудник отеля A по `request_id` брони отеля B получает 004, бронь B цела."""
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other")
        session.add(other)
        await session.flush()
        other_id = other.id
    other_stand = await _stand(other_id)
    foreign = await _book(other_stand, clock)
    await _login(client, portal_hotel, ("fnb",), "Айгерим")

    response = await _answer(client, foreign.request_id, {"answer": "confirmed"})
    with tenant_context(other_id):
        kept = await table_booking_api.get_booking_by_request(foreign.request_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_NOT_FOUND
    assert kept is not None and kept.answer is None
