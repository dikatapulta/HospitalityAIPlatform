"""«Забронировать столик» в веб-чате (spec 0064 §7 п. 4, 6, 7): маршруты гостя за сессией,
флаг выключен — 404 и нет кнопки, чужая бронь — 004, сообщения о шагах брони — по одному
на шаг и на языке брони, «забронирован» — только в тексте «Подтвердить»."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from hospitality.app import create_app
from hospitality.channels.common.models import RequestOrigin
from hospitality.channels.web.table import messages
from hospitality.channels.web.table.texts import MESSAGES, PAGE_TEXTS, day_text
from hospitality.channels.web.tests.conftest import HOTEL_SLUG, RECEPTION_PHONE, ROOM, WebHotel
from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.models import User
from hospitality.platform.table_booking_config import RestaurantConfig, TableBookingConfig
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.events import deliver_pending_events
from hospitality.shared.tenancy import tenant_context

BASE = f"/g/{HOTEL_SLUG}/{ROOM}"
ZONE = ZoneInfo("Asia/Almaty")
TERRACE = RestaurantConfig(
    key="terrace",
    names=LocalizedText(en="Terrace", ru="Терраса", kk="Терраса"),
    category_key="fnb",
    first_seating=time(12, 0),
    last_seating=time(22, 0),
)


async def _store_config(tenant_id: uuid.UUID, booking: TableBookingConfig | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                reception_phone=RECEPTION_PHONE,
                table_booking=booking,
            ),
        )


@pytest.fixture
async def table_hotel(web_hotel: WebHotel) -> WebHotel:
    await _store_config(
        web_hotel.tenant_id, TableBookingConfig(enabled=True, restaurants=(TERRACE,))
    )
    with tenant_context(web_hotel.tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="fnb", name="F&B")
        )
    return web_hotel


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test") as c:
        yield c


async def _enter(client: AsyncClient, code: str, base: str = BASE) -> None:
    response = await client.post(f"{base}/session", json={"code": code})
    assert response.status_code == 200, response.text


def _tomorrow() -> str:
    return (utc_now().astimezone(ZONE).date() + timedelta(days=1)).isoformat()


def _body(*, key: str = "key-0001", at: str = "19:00", language: str = "en") -> dict[str, object]:
    return {
        "client_key": key,
        "restaurant_key": "terrace",
        "date": _tomorrow(),
        "time": at,
        "party_size": 4,
        "guest_name": "Wang Li",
        "wishes": "high chair",
        "language": language,
    }


async def test_disabled_table_booking_has_no_button_and_404(
    client: AsyncClient, web_hotel: WebHotel
) -> None:
    await _enter(client, web_hotel.access_code)
    chat = await client.get(BASE)
    page = await client.get(f"{BASE}/table")
    data = await client.get(f"{BASE}/table/data")

    assert chat.status_code == 200 and 'id="table-link"' not in chat.text
    assert page.status_code == data.status_code == 404
    assert data.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_DISABLED


async def test_guest_pages_need_the_session(client: AsyncClient, table_hotel: WebHotel) -> None:
    chat = await client.get(BASE)
    page = await client.get(f"{BASE}/table", follow_redirects=False)
    data = await client.get(f"{BASE}/table/data")

    assert 'id="table-link"' in chat.text and "Забронировать столик" in chat.text
    assert page.status_code == 303 and page.headers["location"] == BASE
    assert data.status_code == 401

    await _enter(client, table_hotel.access_code)
    page = await client.get(f"{BASE}/table")
    assert page.status_code == 200 and 'id="table-texts"' in page.text


async def test_request_and_cancel_over_http(client: AsyncClient, table_hotel: WebHotel) -> None:
    await _enter(client, table_hotel.access_code)
    data = (await client.get(f"{BASE}/table/data")).json()
    created = await client.post(f"{BASE}/table/bookings", json=_body())
    repeated = await client.post(f"{BASE}/table/bookings", json=_body())
    booking_id = created.json()["id"]
    fetched = await client.get(f"{BASE}/table/bookings/{booking_id}")
    listed = (await client.get(f"{BASE}/table/data")).json()["bookings"]
    cancelled = await client.post(f"{BASE}/table/bookings/{booking_id}/cancel")
    again = await client.post(f"{BASE}/table/bookings/{booking_id}/cancel")
    off_slot = await client.post(f"{BASE}/table/bookings", json=_body(key="key-0002", at="19:10"))

    assert data["room_number"] == ROOM and [r["key"] for r in data["restaurants"]] == ["terrace"]
    assert any(day["date"] == _tomorrow() for day in data["restaurants"][0]["days"])
    assert created.status_code == 201 and repeated.status_code == 200
    assert repeated.json()["id"] == booking_id
    assert fetched.json()["stage"] == "waiting" and fetched.json()["local_time"] == "19:00"
    assert [b["id"] for b in listed] == [booking_id]
    assert cancelled.json()["stage"] == "cancelled"
    assert again.status_code == 409
    assert again.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE
    assert off_slot.status_code == 422
    assert off_slot.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_INVALID


async def test_booking_of_another_stay_is_not_found(table_hotel: WebHotel) -> None:
    with tenant_context(table_hotel.tenant_id):
        neighbour = await check_in(
            StayCheckIn(
                room_number="102",
                check_out_at=utc_now() + timedelta(days=1),
                guest_display_name="Anna",
            )
        )
    app = create_app()
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as guest,
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other,
    ):
        await _enter(guest, table_hotel.access_code)
        await _enter(other, neighbour.access_code, f"/g/{HOTEL_SLUG}/102")
        booking = await guest.post(f"{BASE}/table/bookings", json=_body())
        peek = await other.get(f"/g/{HOTEL_SLUG}/102/table/bookings/{booking.json()['id']}")
        cancel = await other.post(
            f"/g/{HOTEL_SLUG}/102/table/bookings/{booking.json()['id']}/cancel"
        )

    assert booking.status_code == 201
    assert peek.status_code == cancel.status_code == 404
    assert peek.json()["error"]["code"] == table_booking_api.ERR_TABLEBOOKING_NOT_FOUND


async def _outbound(client: AsyncClient) -> list[str]:
    response = await client.get(f"{BASE}/messages")
    assert response.status_code == 200
    return [m["text"] for m in response.json()["messages"] if m["direction"] == "outbound"]


async def test_each_step_message_once_in_booking_language(
    client: AsyncClient, table_hotel: WebHotel
) -> None:
    """Запрос → ответ ресторана словами шага; закрыта общим путём — «не ответил»;
    отмена гостем — молча; заявка «Гость отменил бронь» — не бронь, подписчик молчит.
    Бронь не попадает в `request_origins` (§7 п. 6): общий уведомитель её не видит."""
    messages.register()
    async with platform_session_scope() as session:
        user = User(display_name="Aigerim")
        session.add(user)
        await session.flush()
        staff = requests_api.ActingUser(user_id=user.id, display_name="Aigerim")
    await _enter(client, table_hotel.access_code)
    answer = table_booking_api.BookingAnswer
    plan = [
        ("key-0001", "en", answer.CONFIRMED),
        ("key-0002", "ru", answer.OTHER_TIME),
        ("key-0003", "kk", answer.NO_SEATS),
    ]
    booked: list[dict[str, str]] = []
    for key, language, value in plan:
        booking = (
            await client.post(f"{BASE}/table/bookings", json=_body(key=key, language=language))
        ).json()
        booked.append(booking)
        with tenant_context(table_hotel.tenant_id):
            await table_booking_api.answer_booking(
                uuid.UUID(booking["request_id"]),
                value,
                time(20, 30) if value is answer.OTHER_TIME else None,
                acting_user=staff,
            )
    closed = (await client.post(f"{BASE}/table/bookings", json=_body(key="key-0004"))).json()
    dropped = (await client.post(f"{BASE}/table/bookings", json=_body(key="key-0005"))).json()
    with tenant_context(table_hotel.tenant_id):
        await requests_api.change_request_status(
            uuid.UUID(closed["request_id"]), requests_api.RequestStatus.CANCELLED
        )
        await table_booking_api.cancel_by_guest(uuid.UUID(dropped["id"]), table_hotel.stay_id)
        await table_booking_api.cancel_by_guest(uuid.UUID(booked[0]["id"]), table_hotel.stay_id)
        while await deliver_pending_events():
            pass
        async with session_scope() as session:
            origins = await session.scalar(select(func.count()).select_from(RequestOrigin))
    for _ in range(2):  # повтор доставки — второго сообщения нет (P-8)
        with tenant_context(table_hotel.tenant_id):
            await messages.notify_booking_step(
                uuid.UUID(booked[0]["request_id"]), messages.BookingStep.CLOSED
            )

    day = utc_now().astimezone(ZONE).date() + timedelta(days=1)
    en, ru, kk = day_text(day, "en"), day_text(day, "ru"), day_text(day, "kk")
    texts = await _outbound(client)
    assert origins == 0
    assert texts == [
        f'Request sent to the restaurant "Terrace": {en}, 19:00, guests: 4. This is not a '
        "booking yet — the restaurant will check for free tables and reply here.",
        f'Your table is booked: restaurant "Terrace", {en}, 19:00, guests: 4. We look forward '
        "to seeing you!",
        f"Запрос отправлен в ресторан «Терраса»: {ru}, 19:00, гостей — 4. Это ещё не бронь: "
        "ресторан проверит места и ответит здесь.",
        f"В ресторане «Терраса» на 19:00 мест нет — ресторан предлагает 20:30, {ru}. Подходит — "
        "откройте «🍽 Забронировать столик» вверху чата и отправьте запрос на 20:30 одной "
        "кнопкой.",
        f"Сұраныс «Терраса» мейрамханасына жіберілді: {kk}, 19:00, қонақ саны — 4. Бұл әлі "
        "бронь емес: мейрамхана бос орындарды тексеріп, осы жерде жауап береді.",
        f"Өкінішке қарай, «Терраса» мейрамханасында {kk}, 19:00 уақытына орын жоқ. Басқа "
        "уақытты немесе басқа мейрамхананы таңдап көріңіз.",
        f'Request sent to the restaurant "Terrace": {en}, 19:00, guests: 4. This is not a '
        "booking yet — the restaurant will check for free tables and reply here.",
        f'Request sent to the restaurant "Terrace": {en}, 19:00, guests: 4. This is not a '
        "booking yet — the restaurant will check for free tables and reply here.",
        f'The restaurant "Terrace" did not reply to your request for {en}, 19:00. Call the '
        "restaurant or write to us — we'll help.",
    ]


@pytest.mark.parametrize(
    ("language", "word"), [("ru", "забронирован"), ("kk", "брондалды"), ("en", "booked")]
)
def test_booked_word_only_in_the_confirm_texts(language: str, word: str) -> None:
    """§2.2, §7 п. 4: до ответа ресторана это запрос — «забронирован» только у «Подтвердить»."""
    index = ("ru", "kk", "en").index(language)
    page = {key for key, texts in PAGE_TEXTS.items() if word in texts[index].casefold()}
    chat = {key for key, texts in MESSAGES.items() if word in texts[index].casefold()}

    assert page == {"stage_confirmed"}
    assert chat == {"confirmed"}
