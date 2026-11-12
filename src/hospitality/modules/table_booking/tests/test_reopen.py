"""Возврат отвеченного запроса столика в работу (#998): ответ ресторана снимается.

После `requests.reopen_request` подписчик снимает ответ — бронь снова «ждёт», и
ресторан отвечает иначе. Повтор доставки события после нового ответа его не трогает
(P-8); запрос, закрытый без ответа, и заявка не бронь — без изменений.
"""

from __future__ import annotations

from datetime import time

import pytest
from prometheus_client import REGISTRY

from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.modules.table_booking.tests.conftest import Clock, Hotel, create, place
from hospitality.modules.table_booking.tests.test_answers import answer
from hospitality.shared.tenancy import tenant_context

Answer = table_booking_api.BookingAnswer
Stage = table_booking_api.BookingStage
Status = requests_api.RequestStatus


def _reopened(booking: table_booking_api.BookingRead) -> requests_api.RequestStatusChanged:
    return requests_api.RequestStatusChanged(
        request_id=booking.request_id, old_status=Status.DONE, new_status=Status.IN_PROGRESS
    )


def _withdrawn_count(hotel: Hotel) -> float:
    labels = {"tenant_id": str(hotel.tenant_id), "outcome": "answer_withdrawn"}
    return REGISTRY.get_sample_value("table_bookings_total", labels) or 0.0


async def test_reopen_withdraws_the_answer_and_a_new_one_is_given(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(hotel, create(clock))
    await answer(hotel, booking, Answer.CONFIRMED)
    before = _withdrawn_count(hotel)

    with tenant_context(hotel.tenant_id):
        await requests_api.reopen_request(booking.request_id)
        await table_booking_api.withdraw_answer_on_reopen(_reopened(booking))
        waiting = await table_booking_api.get_booking_by_request(booking.request_id)
    again = await answer(hotel, booking, Answer.NO_SEATS)
    with tenant_context(hotel.tenant_id):
        # Повтор доставки после нового ответа и закрытия — ответ остаётся (P-8).
        await table_booking_api.withdraw_answer_on_reopen(_reopened(booking))
        kept = await table_booking_api.get_booking_by_request(booking.request_id)
        request = await requests_api.get_request(booking.request_id)

    assert waiting is not None and waiting.stage is Stage.WAITING and waiting.answer is None
    assert again.stage is Stage.NO_SEATS
    assert kept is not None and kept.stage is Stage.NO_SEATS
    assert request.status is Status.DONE and request.reopen_count == 1
    assert _withdrawn_count(hotel) == before + 1


async def test_other_transitions_and_non_bookings_are_left_alone(
    hotel: Hotel, clock: Clock
) -> None:
    booking = await place(hotel, create(clock))
    await answer(hotel, booking, Answer.CONFIRMED)
    with tenant_context(hotel.tenant_id):
        towels = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=hotel.other_id,
                origin=requests_api.ServiceRequestOrigin.GUEST_FORM,
                summary="полотенца",
                room_number="101",
            )
        )
        # Не возврат — ответ не трогается, хоть статус и «в работе».
        await table_booking_api.withdraw_answer_on_reopen(
            requests_api.RequestStatusChanged(
                request_id=booking.request_id, old_status=Status.NEW, new_status=Status.IN_PROGRESS
            )
        )
        await table_booking_api.withdraw_answer_on_reopen(
            requests_api.RequestStatusChanged(
                request_id=towels.id, old_status=Status.DONE, new_status=Status.IN_PROGRESS
            )
        )
        current = await table_booking_api.get_booking_by_request(booking.request_id)

    assert current is not None and current.stage is Stage.CONFIRMED


@pytest.mark.parametrize("value", [Answer.CONFIRMED, Answer.OTHER_TIME])
async def test_withdrawn_answer_clears_its_time_and_author(
    hotel: Hotel, clock: Clock, value: Answer
) -> None:
    booking = await place(hotel, create(clock))
    proposed = time(20, 30) if value is Answer.OTHER_TIME else None
    await answer(hotel, booking, value, proposed_time=proposed)

    with tenant_context(hotel.tenant_id):
        await requests_api.reopen_request(booking.request_id)
        await table_booking_api.withdraw_answer_on_reopen(_reopened(booking))
        request = await requests_api.get_request(booking.request_id)
        staff = await table_booking_api.bookings_for_requests([request])

    (row,) = staff.values()
    assert row.answer is None and row.proposed_time is None
    assert row.answered_at is None and row.answered_by_display_name is None
    assert row.can_answer is True
