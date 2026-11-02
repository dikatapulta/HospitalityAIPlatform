"""Что бот знает о продлении: `get_extension_outlook` по Fake-адаптеру (spec 0059 §2.3, §5 п. 3).

Тест на верхнем уровне, как `test_pms_sync.py`: Fake живёт в `integrations/`,
а тесты модуля импортировать адаптеры не вправе (контракт 2 import-linter).
«Сейчас» задаёт тест (`now=`): срок Stay и возраст файла считаются от него.
Фикстуры — из `test_pms_sync.py`; F811 отключён на файл: параметр-фикстура
обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from structlog.testing import capture_logs

from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.guests import api as guests_api
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.ports.pms import PmsNightAvailability
from hospitality.shared.tenancy import tenant_context
from tests.test_pms_sync import (  # noqa: F401  (фикстуры для pytest)
    add_hotel,
    hotel,
    noon,
    pms,
    today,
)


def free_rooms(first_night: date, *counts: int) -> list[PmsNightAvailability]:
    """Ночи подряд с `first_night`: по числу свободных номеров на каждую."""
    return [
        PmsNightAvailability(night=first_night + timedelta(days=offset), free_rooms=count)
        for offset, count in enumerate(counts)
    ]


async def stay_until(
    hotel: uuid.UUID, departure: date, *, room: str = "305", hour: int = 12
) -> uuid.UUID:
    """Ручное заселение до `departure` в `hour` по поясу отеля; вернуть id Stay."""
    with tenant_context(hotel):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=noon(departure, hour))
        )
    return result.stay.id


async def outlook(
    hotel: uuid.UUID, stay_id: uuid.UUID, now: datetime
) -> guests_api.StayExtensionOutlook | None:
    with tenant_context(hotel):
        return await guests_api.get_extension_outlook(stay_id, now=now)


# Выезд — через два дня; «сейчас» — утро накануне выезда по поясу отеля.
DEPARTURE_IN_DAYS = 2


def morning_before_departure() -> datetime:
    return noon(today() + timedelta(days=DEPARTURE_IN_DAYS - 1), hour=9)


async def test_free_nights_after_departure(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure, hour=14)
    now = morning_before_departure()
    taken_at = now - timedelta(minutes=10)
    pms.set_availability(hotel, free_rooms(today(), 3, 5, 8, 2, 1), taken_at=taken_at)

    result = await outlook(hotel, stay_id, now)

    assert result == guests_api.StayExtensionOutlook(
        today=departure - timedelta(days=1),
        departure=departure,
        checkout_hour=14,  # час выезда самого Stay, а не расчётный час отеля
        updated_at=taken_at,
        known_through=today() + timedelta(days=4),
        full_nights=(),
    )


async def test_full_nights_from_departure_on(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Ночи без мест до выезда гостя продлению не мешают и в ответ не попадают."""
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    now = morning_before_departure()
    # Ночь перед выездом — 0, ночь выезда — 4, дальше овербукинг −2 и 0.
    pms.set_availability(
        hotel, free_rooms(departure - timedelta(days=1), 0, 4, -2, 0, 7), taken_at=now
    )

    result = await outlook(hotel, stay_id, now)

    assert result is not None
    assert result.full_nights == (departure + timedelta(days=1), departure + timedelta(days=2))


async def test_departure_today(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    now = noon(departure, hour=8)
    pms.set_availability(hotel, free_rooms(departure, 0, 6), taken_at=now)

    result = await outlook(hotel, stay_id, now)

    assert result is not None
    assert (result.today, result.departure, result.full_nights) == (
        departure,
        departure,
        (departure,),
    )


async def test_stale_or_missing_file_is_v1(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    now = morning_before_departure()
    assert await outlook(hotel, stay_id, now) is None  # файлов ещё не было

    pms.set_availability(hotel, free_rooms(today(), 5, 5, 5), taken_at=now - timedelta(minutes=60))
    assert await outlook(hotel, stay_id, now) is not None  # ровно час — ещё свежий
    pms.set_availability(hotel, free_rooms(today(), 5, 5, 5), taken_at=now - timedelta(minutes=61))
    assert await outlook(hotel, stay_id, now) is None


async def test_file_not_covering_departure_night_is_v1(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    now = morning_before_departure()

    pms.set_availability(hotel, free_rooms(departure - timedelta(days=2), 5, 5), taken_at=now)
    assert await outlook(hotel, stay_id, now) is None  # кончается до ночи выезда
    pms.set_availability(hotel, free_rooms(departure + timedelta(days=1), 5, 5), taken_at=now)
    assert await outlook(hotel, stay_id, now) is None  # начинается после неё
    pms.set_availability(hotel, [], taken_at=now)
    assert await outlook(hotel, stay_id, now) is None
    pms.set_availability(hotel, free_rooms(departure, 0), taken_at=now)
    result = await outlook(hotel, stay_id, now)
    assert result is not None
    assert (result.known_through, result.full_nights) == (departure, (departure,))


async def test_closed_or_overdue_stay_is_v1(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    closed_id = await stay_until(hotel, departure, room="410")
    with tenant_context(hotel):
        await guests_api.check_out(closed_id)
    overdue = noon(departure, hour=12)  # ровно в час выезда доступ гостя гаснет
    pms.set_availability(hotel, free_rooms(today(), *([5] * 10)), taken_at=overdue)

    assert await outlook(hotel, closed_id, morning_before_departure()) is None
    assert await outlook(hotel, stay_id, overdue) is None
    assert await outlook(hotel, uuid.uuid4(), overdue) is None


async def test_manual_hotel_is_v1_even_with_a_file(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    manual = await add_hotel("hotel-manual", pms_source="manual")
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(manual, departure)
    now = morning_before_departure()
    pms.set_availability(manual, free_rooms(today(), 5, 5, 5), taken_at=now)

    assert await outlook(manual, stay_id, now) is None


async def test_hotel_without_config_is_v1_not_a_failure(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Онбординг не завершён: ход гостя идёт без блока, а не падает (ERR-PLATFORM-*)."""
    async with platform_session_scope() as session:
        bare = Tenant(slug="hotel-bare", name="hotel-bare")
        session.add(bare)
        await session.flush()
        bare_id = bare.id
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(bare_id, departure)
    now = morning_before_departure()
    pms.set_availability(bare_id, free_rooms(today(), 5, 5, 5), taken_at=now)

    with capture_logs() as logs:
        assert await outlook(bare_id, stay_id, now) is None
    assert [entry["outcome"] for entry in logs if entry["event"] == "stay_extension_outlook"] == [
        "config_unavailable"
    ]


async def test_other_hotel_file_is_not_seen(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    other = await add_hotel("hotel-other")
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    now = morning_before_departure()
    pms.set_availability(other, free_rooms(today(), 5, 5, 5), taken_at=now)

    assert await outlook(hotel, stay_id, now) is None
    # Stay первого отеля из контекста второго недостижим (RLS), даже с файлом у второго.
    assert await outlook(other, stay_id, now) is None


async def test_log_names_the_outcome_without_dates_or_room(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    departure = today() + timedelta(days=DEPARTURE_IN_DAYS)
    stay_id = await stay_until(hotel, departure)
    now = morning_before_departure()

    with capture_logs() as logs:
        await outlook(hotel, stay_id, now)
        pms.set_availability(hotel, free_rooms(today(), 5, 5, 0), taken_at=now)
        await outlook(hotel, stay_id, now)
        pms.set_availability(hotel, free_rooms(today(), 5, 5, 5), taken_at=now)
        await outlook(hotel, stay_id, now)

    entries = [entry for entry in logs if entry["event"] == "stay_extension_outlook"]
    assert [entry["outcome"] for entry in entries] == ["no_file", "full_nights", "free"]
    for entry in entries:
        assert set(entry) - {"event", "log_level", "module"} == {"outcome"}, entry
