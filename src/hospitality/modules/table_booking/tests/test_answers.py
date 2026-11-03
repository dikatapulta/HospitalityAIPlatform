"""Ответ ресторана и отмена гостем (spec 0064 §7 п. 3, 5, 6; ревью PR #711 Н-2–Н-4):
заявка `done` из `new` и `in_progress`, повтор и иной ответ, граница службы, «Другое
время», ответ без флага, отмены до и после «Подтвердить», стадия «не ответил»."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.modules.table_booking.tests.conftest import (
    Clock,
    Hotel,
    configure_tenant,
    create,
    expect_code,
    place,
    table_booking_config,
)
from hospitality.platform.table_booking_config import TableBookingConfig
from hospitality.shared.tenancy import tenant_context

Answer = table_booking_api.BookingAnswer
Stage = table_booking_api.BookingStage


async def answer(
    hotel: Hotel, booking: table_booking_api.BookingRead, value: Answer, **kwargs: Any
) -> table_booking_api.BookingRead:
    with tenant_context(hotel.tenant_id):
        return await table_booking_api.answer_booking(
            booking.request_id, value, acting_user=hotel.staff, **kwargs
        )


async def request_of(hotel: Hotel, booking: table_booking_api.BookingRead) -> Any:
    with tenant_context(hotel.tenant_id):
        return await requests_api.get_request(booking.request_id)


@pytest.mark.parametrize("value", list(Answer))
async def test_answer_closes_new_request_by_the_same_staff(
    hotel: Hotel, clock: Clock, value: Answer
) -> None:
    booking = await place(hotel, create(clock))
    proposed = time(20, 30) if value is Answer.OTHER_TIME else None

    answered = await answer(hotel, booking, value, proposed_time=proposed)
    repeated = await answer(hotel, booking, value, proposed_time=proposed)
    request = await request_of(hotel, booking)

    assert answered.stage is Stage(value.value) and repeated == answered
    assert answered.proposed_time == ("20:30" if proposed else None)
    assert request.status is requests_api.RequestStatus.DONE
    assert request.claimed_by_display_name == "Айгуль" and request.claimed_at is not None
    assert not answered.cancellable or value is Answer.CONFIRMED


async def test_answer_closes_taken_request_and_refuses_a_different_second_answer(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(hotel, create(clock))
    with tenant_context(hotel.tenant_id):
        await requests_api.change_request_status(
            booking.request_id, requests_api.RequestStatus.IN_PROGRESS
        )

    await answer(hotel, booking, Answer.CONFIRMED)

    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.DONE
    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE,
        answer(hotel, booking, Answer.NO_SEATS),
    )


async def test_foreign_service_cannot_answer(hotel: Hotel, clock: Clock) -> None:
    booking = await place(hotel, create(clock))

    await expect_code(
        requests_api.ERR_REQUESTS_FOREIGN_SERVICE,
        answer(hotel, booking, Answer.CONFIRMED, allowed_category_ids={hotel.other_id}),
    )
    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.NEW
    owned = await answer(hotel, booking, Answer.CONFIRMED, allowed_category_ids={hotel.fnb_id})
    assert owned.stage is Stage.CONFIRMED


@pytest.mark.parametrize(
    ("value", "proposed"),
    [
        (Answer.OTHER_TIME, None),  # «Другое время» без часа
        (Answer.CONFIRMED, time(20, 30)),  # час у иного ответа
        (Answer.OTHER_TIME, time(22, 30)),  # вне часов ресторана
        (Answer.OTHER_TIME, time(20, 10)),  # не слот
        (Answer.OTHER_TIME, time(11, 15)),  # ближе «сейчас + 30 мин» (Н-2)
    ],
)
async def test_bad_other_time_is_422(
    hotel: Hotel, clock: Clock, value: Answer, proposed: time | None
) -> None:
    booking = await place(hotel, create(clock))

    await expect_code("ERR-TABLEBOOKING-002", answer(hotel, booking, value, proposed_time=proposed))
    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.NEW


async def test_answer_after_the_booking_hour_is_refused(hotel: Hotel, clock: Clock) -> None:
    """Н-2: «Подтвердить» после часа брони — «Ждём вас!» на прошедшее время."""
    booking = await place(hotel, create(clock, at=time(12, 0)))
    clock.now = clock.local(0, time(12, 5))

    with tenant_context(hotel.tenant_id):
        late = await table_booking_api.get_booking_for_stay(booking.id, hotel.stay_id)
    assert late.stage is Stage.NO_ANSWER and not late.cancellable
    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE,
        answer(hotel, booking, Answer.CONFIRMED),
    )


async def test_answer_does_not_depend_on_the_flag(hotel: Hotel, clock: Clock) -> None:
    """Н-3: после выключения открытые запросы закрываются; без ресторана в конфиге
    «Другое время» — 002 (часов не знаем), остальные ответы — как раньше."""
    first = await place(hotel, create(clock, key="key-0001"))
    second = await place(hotel, create(clock, key="key-0002"))
    await configure_tenant(hotel.tenant_id, table_booking_config(enabled=False))

    assert (await answer(hotel, first, Answer.CONFIRMED)).stage is Stage.CONFIRMED

    await configure_tenant(hotel.tenant_id, TableBookingConfig(enabled=True))
    await expect_code(
        "ERR-TABLEBOOKING-002",
        answer(hotel, second, Answer.OTHER_TIME, proposed_time=time(20, 0)),
    )
    assert (await answer(hotel, second, Answer.NO_SEATS)).restaurant_names.en == "Terrace"


async def test_guest_cancels_before_answer_and_restaurant_loses(hotel: Hotel, clock: Clock) -> None:
    booking = await place(hotel, create(clock))
    with tenant_context(hotel.tenant_id):
        cancelled = await table_booking_api.cancel_by_guest(booking.id, hotel.stay_id)

    assert cancelled.stage is Stage.CANCELLED and not cancelled.cancellable
    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.CANCELLED
    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE,
        answer(hotel, booking, Answer.CONFIRMED),
    )
    with tenant_context(hotel.tenant_id):
        await expect_code(
            table_booking_api.ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE,
            table_booking_api.cancel_by_guest(booking.id, hotel.stay_id),
        )


async def test_guest_cancel_after_no_seats_or_after_the_hour_is_006(
    hotel: Hotel, clock: Clock
) -> None:
    no_seats = await place(hotel, create(clock, key="key-0001"))
    await answer(hotel, no_seats, Answer.NO_SEATS)
    confirmed = await place(hotel, create(clock, at=time(12, 0), key="key-0002"))
    await answer(hotel, confirmed, Answer.CONFIRMED)
    clock.now = clock.local(0, time(12, 0))

    with tenant_context(hotel.tenant_id):
        for booking in (no_seats, confirmed):
            await expect_code(
                table_booking_api.ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE,
                table_booking_api.cancel_by_guest(booking.id, hotel.stay_id),
            )


async def test_cancel_of_confirmed_booking_sends_one_request_to_the_restaurant(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(hotel, create(clock))
    await answer(hotel, booking, Answer.CONFIRMED)
    with tenant_context(hotel.tenant_id):
        cancelled = await table_booking_api.cancel_by_guest(booking.id, hotel.stay_id)
        await expect_code(
            table_booking_api.ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE,
            table_booking_api.cancel_by_guest(booking.id, hotel.stay_id),
        )
        open_requests = await requests_api.list_open_requests(limit=50)

    assert cancelled.stage is Stage.CANCELLED and cancelled.answer is Answer.CONFIRMED
    assert len(open_requests) == 1
    notice = open_requests[0]
    assert notice.category_id == hotel.fnb_id
    assert notice.summary.startswith("Гость отменил бронь: Терраса,")
    assert notice.summary.endswith("4 гост., № 101 — уберите из книги броней")


async def test_crash_between_cancel_mark_and_request_is_finished_by_the_page(
    hotel: Hotel, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н-4: гость прочтёт «мы передали это ресторану» — ресторан обязан узнать."""
    booking = await place(hotel, create(clock))
    await answer(hotel, booking, Answer.CONFIRMED)
    real_create = requests_api.create_request
    calls = {"n": 0}

    async def crash_once(*args: Any, **kwargs: Any) -> requests_api.ServiceRequestRead:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker died between mark and request")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(requests_api, "create_request", crash_once)
    with tenant_context(hotel.tenant_id):
        with pytest.raises(RuntimeError):
            await table_booking_api.cancel_by_guest(booking.id, hotel.stay_id)
        page = await table_booking_api.list_bookings_for_stay(hotel.stay_id)
        again = await table_booking_api.list_bookings_for_stay(hotel.stay_id)
        open_requests = await requests_api.list_open_requests(limit=50)

    assert page[0].stage is Stage.CANCELLED and again == page
    assert len(open_requests) == 1 and open_requests[0].summary.startswith("Гость отменил бронь")


async def test_request_closed_without_answer_reads_as_no_answer(hotel: Hotel, clock: Clock) -> None:
    """§2.3: заявку закрыли общим путём (API) — гостю честное «не ответил», не молчание."""
    booking = await place(hotel, create(clock))
    with tenant_context(hotel.tenant_id):
        await requests_api.change_request_status(
            booking.request_id, requests_api.RequestStatus.CANCELLED
        )
        read = await table_booking_api.get_booking_for_stay(booking.id, hotel.stay_id)

    assert read.stage is Stage.NO_ANSWER and not read.cancellable


async def test_failed_transition_rolls_back_answer_and_guest_mark(
    hotel: Hotel, clock: Clock
) -> None:
    """Заявку закрыли общим путём, а ответ или отмена пришли следом: переход не прошёл —
    ни ответа, ни отметки отмены в брони не остаётся (стадия — «не ответил»)."""
    booking = await place(hotel, create(clock))
    with tenant_context(hotel.tenant_id):
        await requests_api.change_request_status(
            booking.request_id, requests_api.RequestStatus.CANCELLED
        )

    await expect_code(
        table_booking_api.ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE,
        answer(hotel, booking, Answer.CONFIRMED),
    )
    with tenant_context(hotel.tenant_id):
        await expect_code(
            table_booking_api.ERR_TABLEBOOKING_CANCEL_NOT_APPLICABLE,
            table_booking_api.cancel_by_guest(booking.id, hotel.stay_id),
        )
        read = await table_booking_api.get_booking_for_stay(booking.id, hotel.stay_id)

    assert read.answer is None and read.cancelled_by_guest_at is None
    assert read.stage is Stage.NO_ANSWER
