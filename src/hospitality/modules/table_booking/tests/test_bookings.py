"""Запрос гостя на столик (spec 0064 §7 п. 1, 2, 7): слоты и часы отеля, отказы 001–003,
двойное «Отправить» и сбой между бронью и заявкой, лимиты проживания, ретеншн, изоляция."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import time, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.modules.table_booking.slots import slot_times
from hospitality.modules.table_booking.tests.conftest import (
    TERRACE,
    ZONE,
    Clock,
    Hotel,
    configure_tenant,
    create,
    expect_code,
    make_hotel,
    place,
    table_booking_config,
)
from hospitality.platform.table_booking_config import RestaurantConfig
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context


def test_slots_follow_seating_hours_by_thirty_minutes() -> None:
    late = RestaurantConfig.model_validate(
        {**TERRACE.model_dump(), "first_seating": time(12, 15), "last_seating": time(13, 30)}
    )

    assert slot_times(TERRACE)[0] == time(12, 0) and slot_times(TERRACE)[-1] == time(22, 0)
    assert len(slot_times(TERRACE)) == 21
    assert slot_times(late) == [time(12, 15), time(12, 45), time(13, 15)]


async def test_options_start_thirty_minutes_from_now_and_end_on_checkout_day(
    hotel: Hotel, clock: Clock
) -> None:
    clock.now = clock.local(0, time(18, 40))
    with tenant_context(hotel.tenant_id):
        options = await table_booking_api.get_options(hotel.stay_id)
    days = options.restaurants[0].days

    assert options.today == clock.today()
    assert days[0].date == clock.today() and days[0].slots[0] == "19:30"
    assert days[1].slots[0] == "12:00" and days[1].slots[-1] == "22:00"
    assert days[-1].date == (utc_now() + timedelta(days=3)).astimezone(ZONE).date()

    clock.now = clock.local(0, time(21, 45))
    with tenant_context(hotel.tenant_id):
        late = await table_booking_api.get_options(hotel.stay_id)
    assert late.restaurants[0].days[0].date == clock.today() + timedelta(days=1)


async def test_request_creates_service_request_in_restaurant_service(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(
        hotel, create(clock, guest_name=" Wang Li ", wishes="у окна, карта 4111 1111 1111 1111")
    )
    with tenant_context(hotel.tenant_id):
        request = await requests_api.get_request(booking.request_id)

    assert booking.stage is table_booking_api.BookingStage.WAITING
    assert (booking.local_date, booking.local_time) == (clock.today(), "19:00")
    assert booking.booking_at == clock.local(0, time(19, 0))
    assert booking.cancellable and booking.room_number == "101"
    assert request.category_id == hotel.fnb_id
    assert request.origin is requests_api.ServiceRequestOrigin.GUEST_FORM
    assert request.stay_id == hotel.stay_id and request.guest_language == "ru"
    assert request.summary.startswith("Столик: «Терраса»") and "гостей — 4" in request.summary
    assert request.details is not None and "На имя: Wang Li" in request.details
    assert "4111 1111 1111 1111" not in request.details


async def test_double_submit_and_crash_between_booking_and_request_give_one_request(
    hotel: Hotel, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_create = requests_api.create_request
    calls = {"n": 0}

    async def crash_once(*args: Any, **kwargs: Any) -> requests_api.ServiceRequestRead:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker died between booking and request")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(requests_api, "create_request", crash_once)
    with pytest.raises(RuntimeError):
        await place(hotel, create(clock))
    first = await place(hotel, create(clock))
    repeat = await place(hotel, create(clock))

    with tenant_context(hotel.tenant_id):
        listed = await table_booking_api.list_bookings_for_stay(hotel.stay_id)
        open_requests = await requests_api.list_open_requests(limit=50)
    assert first.id == repeat.id and [b.id for b in listed] == [first.id]
    assert [r.id for r in open_requests] == [first.request_id]


@pytest.mark.parametrize(
    ("day", "at", "party", "code"),
    [
        (0, time(11, 15), 2, "ERR-TABLEBOOKING-002"),  # ближе «сейчас + 30 мин»
        (0, time(12, 10), 2, "ERR-TABLEBOOKING-002"),  # не слот
        (0, time(22, 30), 2, "ERR-TABLEBOOKING-002"),  # после последней посадки
        (4, time(19, 0), 2, "ERR-TABLEBOOKING-002"),  # после дня выезда
        (-1, time(19, 0), 2, "ERR-TABLEBOOKING-002"),  # вчера
        (0, time(19, 0), 9, "ERR-TABLEBOOKING-002"),  # больше max_party
    ],
)
async def test_request_outside_rules_is_rejected(
    hotel: Hotel, clock: Clock, day: int, at: time, party: int, code: str
) -> None:
    await expect_code(code, place(hotel, create(clock, day=day, at=at, party_size=party)))


async def test_fourteen_days_window_even_for_a_long_stay(
    canonical_database: None, clock: Clock
) -> None:
    hotel = await make_hotel("long-stay", nights=30)

    await place(hotel, create(clock, day=14, key="key-in-window"))
    await expect_code(
        "ERR-TABLEBOOKING-002", place(hotel, create(clock, day=15, key="key-out-window"))
    )


async def test_disabled_booking_and_unknown_restaurant_are_001(hotel: Hotel, clock: Clock) -> None:
    await expect_code("ERR-TABLEBOOKING-001", place(hotel, create(clock, restaurant_key="rooftop")))
    await configure_tenant(hotel.tenant_id, table_booking_config(enabled=False))
    await expect_code("ERR-TABLEBOOKING-001", place(hotel, create(clock)))
    with tenant_context(hotel.tenant_id):
        await expect_code("ERR-TABLEBOOKING-001", table_booking_api.get_options(hotel.stay_id))


async def test_three_unanswered_requests_per_stay(hotel: Hotel, clock: Clock) -> None:
    placed = [
        await place(hotel, create(clock, at=time(19, 0), key=f"key-000{i}")) for i in range(3)
    ]
    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_LIMIT, place(hotel, create(clock, key="key-0004"))
    )

    with tenant_context(hotel.tenant_id):
        await table_booking_api.answer_booking(
            placed[0].request_id, table_booking_api.BookingAnswer.NO_SEATS, acting_user=hotel.staff
        )
    await place(hotel, create(clock, key="key-0005"))


async def test_ten_requests_per_hotel_day_cancelled_count_too(hotel: Hotel, clock: Clock) -> None:
    for i in range(10):
        booking = await place(hotel, create(clock, key=f"key-{i:04d}"))
        with tenant_context(hotel.tenant_id):
            await table_booking_api.cancel_by_guest(booking.id, hotel.stay_id)

    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_LIMIT, place(hotel, create(clock, key="key-0010"))
    )


@pytest.mark.parametrize("limit", ["unanswered", "hotel_day"])
async def test_neighbour_stay_at_its_limit_does_not_block_this_stay(
    hotel: Hotel, clock: Clock, limit: str
) -> None:
    """Лимиты — на проживание (§4, Н-6): сосед по отелю на пределе гостю не мешает.

    Проба мутаций #1052: без фильтра по проживанию тесты выше зелёные — в них одно проживание.
    """
    with tenant_context(hotel.tenant_id):
        stay = await check_in(
            StayCheckIn(room_number="102", check_out_at=utc_now() + timedelta(days=3))
        )
    neighbour = replace(hotel, stay_id=stay.stay.id, guest_identity_id=uuid.uuid4())
    for i in range(3 if limit == "unanswered" else 10):
        booking = await place(neighbour, create(clock, key=f"key-n{i:03d}"))
        if limit == "hotel_day":
            with tenant_context(hotel.tenant_id):
                await table_booking_api.cancel_by_guest(booking.id, neighbour.stay_id)
    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_LIMIT, place(neighbour, create(clock, key="key-n-over"))
    )

    booking = await place(hotel, create(clock, key="key-own-stay"))

    assert booking.stage is table_booking_api.BookingStage.WAITING


async def test_retention_detaches_old_bookings_and_drops_abandoned(
    hotel: Hotel, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = await place(hotel, create(clock, key="key-kept"))

    async def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no request")

    monkeypatch.setattr(requests_api, "create_request", crash)
    with pytest.raises(RuntimeError):
        await place(hotel, create(clock, key="key-lost"))
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE table_bookings SET created_at = created_at - interval '2 days'")
            )
        abandoned = await table_booking_api.delete_abandoned_bookings(
            created_before=utc_now() - table_booking_api.ABANDONED_AFTER
        )
        detached = await table_booking_api.detach_expired_bookings(created_before=utc_now())
        detached_again = await table_booking_api.detach_expired_bookings(created_before=utc_now())
        gone = await table_booking_api.get_booking_by_request(kept.request_id)

    assert (abandoned, detached, detached_again) == (1, 1, 0)
    assert gone is not None and gone.restaurant_key == "terrace"
    with tenant_context(hotel.tenant_id):
        assert await table_booking_api.booking_guest_identity(kept.request_id) is None


async def test_other_tenant_does_not_see_the_booking(hotel: Hotel, clock: Clock) -> None:
    booking = await place(hotel, create(clock))
    other = await make_hotel("other-hotel")

    with tenant_context(other.tenant_id):
        await expect_code(
            table_booking_api.ERR_TABLEBOOKING_NOT_FOUND,
            table_booking_api.get_booking_for_stay(booking.id, hotel.stay_id),
        )
        assert await table_booking_api.get_booking_by_request(booking.request_id) is None
        assert await table_booking_api.list_bookings_for_stay(hotel.stay_id) == []
