"""Бронь для кабинета (spec 0064 §6 PR 3): карточка, закрытие без ответа, журнал, Н-1.

Карточка видит только брони (заявка «Гость отменил бронь» и обычная заявка — нет),
«Другое время» — слоты того же дня без запрошенного и не ближе «сейчас + 30 мин».
Час брони прошёл без ответа — ответить нельзя, закрыть можно (иначе заявка висит).
Ответ пишет одну строку журнала «Ответ на бронь столика»; повтор того же ответа
доводит заявку, которую прошлый вызов не закрыл (ревью PR #716, Н-1).
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import answers
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.modules.table_booking.slots import ru_day
from hospitality.modules.table_booking.tests.conftest import (
    Clock,
    Hotel,
    create,
    expect_code,
    place,
)
from hospitality.modules.table_booking.tests.test_answers import answer, request_of
from hospitality.platform import staff_actions
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context

Answer = table_booking_api.BookingAnswer
Stage = table_booking_api.BookingStage
NOT_APPLICABLE = table_booking_api.ERR_TABLEBOOKING_ANSWER_NOT_APPLICABLE


async def cards(
    hotel: Hotel, *bookings: table_booking_api.BookingRead
) -> dict[Any, table_booking_api.StaffBookingRead]:
    with tenant_context(hotel.tenant_id):
        requests = await requests_api.list_open_requests(limit=50)
        closed = await requests_api.list_requests_by_ids([b.request_id for b in bookings])
        return await table_booking_api.bookings_for_requests([*requests, *closed])


async def close(hotel: Hotel, booking: table_booking_api.BookingRead) -> Any:
    with tenant_context(hotel.tenant_id):
        return await table_booking_api.close_unanswered(booking.request_id, acting_user=hotel.staff)


async def journal(hotel: Hotel) -> list[staff_actions.StaffActionRead]:
    now = utc_now()
    with tenant_context(hotel.tenant_id):
        return await staff_actions.list_staff_actions(
            day_start=now - timedelta(days=1),
            day_end=now + timedelta(days=1),
            actor_login=None,
            actions=frozenset({staff_actions.StaffAction.TABLE_BOOKING_ANSWERED}),
            limit=10,
            offset=0,
        )


async def test_card_shows_only_bookings_with_other_time_slots(hotel: Hotel, clock: Clock) -> None:
    booking = await place(hotel, create(clock, at=time(19, 0)))
    with tenant_context(hotel.tenant_id):
        plain = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=hotel.fnb_id,
                origin=requests_api.ServiceRequestOrigin.API,
                summary="полотенца",
            )
        )
        ids = await table_booking_api.booking_request_ids([booking.request_id, plain.id])

    card = (await cards(hotel, booking))[booking.request_id]

    assert set(await cards(hotel, booking)) == {booking.request_id} == ids
    assert card.can_answer and not card.can_close_unanswered
    assert card.day_label == ru_day(clock.today())  # «3 ноября»
    expected = [f"{h:02d}:{m:02d}" for h in range(12, 23) for m in (0, 30) if (h, m) <= (22, 0)]
    assert card.other_time_slots == [slot for slot in expected if slot != "19:00"]
    assert card.answered_by_display_name is None


async def test_other_time_slots_start_thirty_minutes_from_now(hotel: Hotel, clock: Clock) -> None:
    booking = await place(hotel, create(clock, at=time(21, 0)))
    clock.now = clock.local(0, time(20, 10))

    card = (await cards(hotel, booking))[booking.request_id]

    assert card.other_time_slots == ["21:30", "22:00"]


async def test_answer_card_names_staff_and_cancel_request_is_a_plain_card(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(hotel, create(clock))
    await answer(hotel, booking, Answer.CONFIRMED)
    with tenant_context(hotel.tenant_id):
        await table_booking_api.cancel_by_guest(booking.id, hotel.stay_id)

    found = await cards(hotel, booking)

    assert set(found) == {booking.request_id}  # заявка «Гость отменил бронь» — не бронь
    card = found[booking.request_id]
    assert card.stage is Stage.CANCELLED and card.answered_by_display_name == "Айгуль"
    assert not card.can_answer and not card.can_close_unanswered and card.other_time_slots == []


async def test_hour_passed_without_answer_is_closed_not_answered(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(hotel, create(clock, at=time(12, 0)))
    await expect_code(NOT_APPLICABLE, close(hotel, booking))  # час ещё не прошёл
    clock.now = clock.local(0, time(12, 30))

    card = (await cards(hotel, booking))[booking.request_id]
    assert card.stage is Stage.NO_ANSWER and card.can_close_unanswered and not card.can_answer
    await expect_code(NOT_APPLICABLE, answer(hotel, booking, Answer.CONFIRMED))
    closed = await close(hotel, booking)
    request = await request_of(hotel, booking)

    assert closed.stage is Stage.NO_ANSWER and closed.answer is None
    assert request.status is requests_api.RequestStatus.DONE
    assert request.closed_by_display_name == "Айгуль"
    assert not (await cards(hotel, booking))[booking.request_id].can_close_unanswered
    await expect_code(NOT_APPLICABLE, close(hotel, booking))
    assert await journal(hotel) == []  # закрытие — не ответ ресторана


async def test_answered_or_foreign_request_is_not_closed(hotel: Hotel, clock: Clock) -> None:
    answered = await place(hotel, create(clock, at=time(12, 0), key="key-0001"))
    await answer(hotel, answered, Answer.NO_SEATS)
    foreign = await place(hotel, create(clock, at=time(12, 0), key="key-0002"))
    clock.now = clock.local(0, time(12, 30))

    await expect_code(NOT_APPLICABLE, close(hotel, answered))
    with tenant_context(hotel.tenant_id):
        await expect_code(
            requests_api.ERR_REQUESTS_FOREIGN_SERVICE,
            table_booking_api.close_unanswered(
                foreign.request_id, acting_user=hotel.staff, allowed_category_ids={hotel.other_id}
            ),
        )
    assert (await request_of(hotel, foreign)).status is requests_api.RequestStatus.NEW


async def test_answer_writes_one_journal_row(hotel: Hotel, clock: Clock) -> None:
    booking = await place(hotel, create(clock))

    await answer(hotel, booking, Answer.CONFIRMED)
    await answer(hotel, booking, Answer.CONFIRMED)

    rows = await journal(hotel)
    assert len(rows) == 1
    row = rows[0]
    assert row.request_id == booking.request_id and row.request_daily_number == booking.number
    assert row.category_id == hotel.fnb_id and row.room_number == "101"


async def test_repeat_answer_closes_request_left_open_by_a_crash(
    hotel: Hotel, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н-1: ответ записан, переходы заявки упали не `AppError` — повтор того же ответа
    закрывает заявку (гость получит ответ), а не молча возвращает бронь."""
    booking = await place(hotel, create(clock))
    real_close = answers._close_request
    calls = {"n": 0}

    async def crash_once(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database connection lost between answer and request")
        await real_close(*args, **kwargs)

    monkeypatch.setattr(answers, "_close_request", crash_once)
    with pytest.raises(RuntimeError):
        await answer(hotel, booking, Answer.CONFIRMED)
    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.NEW

    repeated = await answer(hotel, booking, Answer.CONFIRMED)

    assert repeated.stage is Stage.CONFIRMED
    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.DONE
    assert len(await journal(hotel)) == 1


async def test_request_closed_by_a_parallel_same_answer_keeps_the_answer(
    hotel: Hotel, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повтор закрывает заявку (Н-1), поэтому два одинаковых ответа в одну секунду —
    оба закрывают: проигравший переход видит `done` и ответ не снимает."""
    booking = await place(hotel, create(clock))
    real_close = answers._close_request

    async def neighbour_first(*args: Any, **kwargs: Any) -> None:
        await real_close(*args, **kwargs)  # соседний вызов с тем же ответом успел раньше
        await real_close(*args, **kwargs)

    monkeypatch.setattr(answers, "_close_request", neighbour_first)
    answered = await answer(hotel, booking, Answer.CONFIRMED)

    assert answered.stage is Stage.CONFIRMED and answered.answer is Answer.CONFIRMED
    assert (await request_of(hotel, booking)).status is requests_api.RequestStatus.DONE


async def test_request_cancelled_between_answer_and_close_drops_the_answer(
    hotel: Hotel, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Заявку отменили общим путём между записью ответа и переходами: ответ снимается,
    005, строки журнала нет — гостю не уйдёт «забронирован» по отменённой заявке."""
    booking = await place(hotel, create(clock))
    real_close = answers._close_request

    async def cancelled_first(*args: Any, **kwargs: Any) -> None:
        await requests_api.change_request_status(
            booking.request_id, requests_api.RequestStatus.CANCELLED
        )
        await real_close(*args, **kwargs)

    monkeypatch.setattr(answers, "_close_request", cancelled_first)
    await expect_code(NOT_APPLICABLE, answer(hotel, booking, Answer.CONFIRMED))
    with tenant_context(hotel.tenant_id):
        read = await table_booking_api.get_booking_by_request(booking.request_id)

    assert read is not None and read.answer is None and read.stage is Stage.NO_ANSWER
    assert await journal(hotel) == []
