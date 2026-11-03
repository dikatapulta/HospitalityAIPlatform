"""Брони столика демо-картины (#752, spec 0064): одна в каждой стадии ответа ресторана.

Все три — у одного проживания (`DEMO_BOOKING_ROOM`): гость этого номера видит на
странице «Ваши брони» все стадии сразу, служба ресторана — запрос в очереди.
Путь — публичный API модуля, как у гостя и кабинета (R-5): `request_booking`
рождает заявку в службе ресторана, `answer_booking` — ответ от имени менеджера
(своих сотрудников у службы ресторана в картине нет).

Личность гостя — выдуманный `uuid5` от проживания: сессии и согласия у гостя
картины нет, заводить их ради демо значило бы записать согласие, которого никто
не давал. Сообщения ресторана гостю ложатся в веб-диалог, который никто не
откроет; страница «Ваши брони» показывает брони по проживанию.

Повтор в тот же день отеля — те же брони (ключ формы — ресторан и день отеля).
Открытый запрос на столик в номере картины, созданный вчера или до `--reset`,
закрывается отменой «дубль», как заявки картины: иначе каждое нажатие кнопки
оставляло бы в очереди ресторана ещё один запрос без ответа. Ответы прошлых
картин остаются (стереть закрытое API не даёт) — у выехавшего проживания.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.shared.errors import AppError
from hospitality.tools.demo_showcase_staff import DemoActors

BookingAnswer = table_booking_api.BookingAnswer

DEMO_BOOKING_ROOM: Final = "2501"
_GUEST_NAMESPACE: Final = uuid.UUID("5b0f6a52-7c1e-4f1d-9a3e-752d0e5f7a52")
_SCAN_LIMIT: Final = 100


@dataclass(frozen=True)
class DemoBooking:
    restaurant: str  # ключ ресторана демо-конфига (`tools/demo_table_booking.py`)
    at: time  # желаемый час: первый слот не раньше него сегодня, иначе завтра
    party: int
    answer: BookingAnswer | None  # None — ждёт ответа ресторана
    wishes: str | None = None


DEMO_BOOKINGS: Final = (
    DemoBooking("terrace", time(19, 30), 2, None),
    DemoBooking("dastarkhan", time(20, 0), 4, BookingAnswer.CONFIRMED, wishes="Детский стульчик"),
    DemoBooking("sky-bar", time(21, 0), 2, BookingAnswer.NO_SEATS),
)


@dataclass(frozen=True)
class BookingsOutcome:
    line: str
    count: int  # броней в картине; 0 — бронь у демо-отеля выключена
    # Рестораны броней, ждущих ответа: их заявки — новые, срок просрочки — по службе ресторана.
    waiting: tuple[str, ...]


def pick_slot(option: table_booking_api.RestaurantOption, at: time) -> tuple[date, time] | None:
    """Первый слот не раньше `at` в ближайший день, где он есть; иначе первый слот вообще."""
    wanted = f"{at:%H:%M}"
    for day in option.days:
        later = [slot for slot in day.slots if slot >= wanted]
        if later:
            return day.date, time.fromisoformat(later[0])
    for day in option.days:
        if day.slots:
            return day.date, time.fromisoformat(day.slots[0])
    return None


async def _close_stale(actors: DemoActors, *, midnight: datetime, reset: bool) -> None:
    """Открытые запросы на столик в номере картины — прошлого дня или прошлой картины."""
    for request in await requests_api.list_open_requests(
        limit=_SCAN_LIMIT, room_number=DEMO_BOOKING_ROOM
    ):
        if not (reset or request.created_at < midnight):
            continue
        if await table_booking_api.get_booking_by_request(request.id) is None:
            continue  # не бронь — заявки номера ведёт ensure_requests
        await requests_api.change_request_status(
            request.id,
            requests_api.RequestStatus.CANCELLED,
            cancel_reason=requests_api.CancelReason.DUPLICATE,
            acting_user=actors.manager,
        )


async def ensure_bookings(
    actors: DemoActors, *, midnight: datetime, today: date, reset: bool
) -> BookingsOutcome:
    """Брони картины у проживания `DEMO_BOOKING_ROOM`: ждёт ответа, подтверждена, «мест нет»."""
    await _close_stale(actors, midnight=midnight, reset=reset)
    stay = await guests_api.find_active_stay(DEMO_BOOKING_ROOM)
    if stay is None:  # pragma: no cover — номер картины заселяет ensure_stays
        return BookingsOutcome(f"Брони столика: номер {DEMO_BOOKING_ROOM} не заселён.", 0, ())
    try:
        options = await table_booking_api.get_options(stay.id)
    except AppError as error:
        if error.code != table_booking_api.ERR_TABLEBOOKING_DISABLED:
            raise
        return BookingsOutcome(
            "Брони столика: у демо-отеля выключены (table_booking в конфиге) — пропущены.", 0, ()
        )
    restaurants = {option.key: option for option in options.restaurants}
    guest = uuid.uuid5(_GUEST_NAMESPACE, str(stay.id))
    created = count = 0
    waiting: list[str] = []
    for spec in DEMO_BOOKINGS:
        option = restaurants.get(spec.restaurant)
        moment = pick_slot(option, spec.at) if option is not None else None
        if moment is None:
            continue  # ресторан убрали из конфига руками или слотов нет до выезда
        placed = await table_booking_api.request_booking(
            stay.id,
            guest,
            table_booking_api.BookingCreate(
                client_key=f"demo-showcase-{spec.restaurant}-{today:%Y%m%d}",
                restaurant_key=spec.restaurant,
                date=moment[0],
                time=moment[1],
                party_size=spec.party,
                wishes=spec.wishes,
                guest_language="ru",
            ),
        )
        count += 1
        if not placed.duplicate:
            created += 1
        if placed.booking.stage is not table_booking_api.BookingStage.WAITING:
            continue  # ответ уже есть (повтор в тот же день) или запрос закрыт руками
        if spec.answer is None:
            waiting.append(spec.restaurant)
        else:
            await table_booking_api.answer_booking(
                placed.booking.request_id, spec.answer, acting_user=actors.manager
            )
    line = (
        f"Брони столика у номера {DEMO_BOOKING_ROOM}: {count} в разных стадиях ответа "
        f"ресторана, создано сейчас — {created}."
    )
    return BookingsOutcome(line, count, tuple(waiting))
