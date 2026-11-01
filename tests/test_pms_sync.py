"""Сверка проживаний со снимком PMS: правила по Fake-адаптеру (spec 0058 §2.2, §5 п. 1).

Каждый тест двигает «дом» Fake-адаптера, выпускает снимок и зовёт тот же шаг,
что воркер (`sync_stays_from_pms`). Тест на верхнем уровне, как контрактный
`test_pms_port.py`: Fake живёт в `integrations/`, а тесты модуля импортировать
адаптеры не вправе (контракт 2 import-linter). Защита, повтор, гонка и алерты —
`test_pms_sync_guards.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.guests import api as guests_api
from hospitality.modules.guests.models import PmsSyncState, Stay, StayAccessCode
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffAction, StaffActionLog, Tenant
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.events import OutboxEvent
from hospitality.shared.ports.pms import pms_or_none, set_pms
from hospitality.shared.tenancy import tenant_context

ZONE = ZoneInfo("Asia/Almaty")


def today() -> date:
    return utc_now().astimezone(ZONE).date()


def noon(day: date, hour: int = 12) -> datetime:
    """Расчётный час дня по поясу отеля — в UTC, как его пишет сверка."""
    return datetime.combine(day, time(hour), tzinfo=ZONE).astimezone(UTC)


@pytest.fixture
def pms() -> Iterator[FakePmsAdapter]:
    """Fake-адаптер в порту на время теста; прежний — обратно (канон `fake_file_storage`)."""
    previous = pms_or_none()
    adapter = FakePmsAdapter()
    set_pms(adapter)
    try:
        yield adapter
    finally:
        if previous is not None:
            set_pms(previous)


async def add_hotel(slug: str, *, pms_source: str = "opera_report") -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        await store_tenant_config(
            session,
            tenant.id,
            TenantConfig.model_validate(
                {
                    "profile": HotelProfile(city="Almaty", country_code="KZ"),
                    "timezone": "Asia/Almaty",
                    "default_language": "ru",
                    "pms_source": pms_source,
                }
            ),
        )
        return tenant.id


@pytest.fixture
async def hotel(canonical_database: None) -> uuid.UUID:
    """Отель со сверкой (`pms_source: opera_report`), расчётный час 12:00."""
    return await add_hotel("hotel-opera")


def book(
    pms: FakePmsAdapter,
    hotel: uuid.UUID,
    room: str,
    ref: str,
    *,
    arrival: date | None = None,
    nights: int = 2,
    guests: int | None = None,
) -> None:
    arrival = arrival or today()
    pms.check_in_room(
        hotel,
        room,
        ref,
        arrival=arrival,
        departure=arrival + timedelta(days=nights),
        guests_count=guests,
    )


async def sync(pms: FakePmsAdapter, hotel: uuid.UUID) -> int:
    """Выгрузка Opera и прогон сверки — то, что делают отель и воркер."""
    pms.publish(hotel)
    return await guests_api.sync_stays_from_pms()


async def stays(hotel: uuid.UUID) -> list[Stay]:
    """Все Stay отеля, по комнате."""
    with tenant_context(hotel):
        async with session_scope() as session:
            return list(await session.scalars(select(Stay).order_by(Stay.room_number)))


async def active(hotel: uuid.UUID) -> dict[str, Stay]:
    return {s.room_number: s for s in await stays(hotel) if s.status.value == "checked_in"}


async def journal(hotel: uuid.UUID) -> list[StaffActionLog]:
    with tenant_context(hotel):
        async with session_scope() as session:
            rows = await session.scalars(
                select(StaffActionLog).order_by(StaffActionLog.occurred_at)
            )
            return list(rows)


async def state(hotel: uuid.UUID) -> PmsSyncState:
    with tenant_context(hotel):
        async with session_scope() as session:
            row = await session.get(PmsSyncState, hotel)
            assert row is not None
            return row


async def manual_check_in(
    hotel: uuid.UUID, room: str, *, checked_in_days_ago: int = 0
) -> guests_api.StayCheckInResult:
    """Заселение из кабинета; `checked_in_days_ago` — заезд «тогда-то» (сдвиг check_in_at)."""
    with tenant_context(hotel):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=noon(today() + timedelta(days=3)))
        )
        if checked_in_days_ago:
            async with session_scope() as session:
                stay = await session.get(Stay, result.stay.id)
                assert stay is not None
                stay.check_in_at = utc_now() - timedelta(days=checked_in_days_ago)
    return result


async def bind_guest(hotel: uuid.UUID, room: str, code: str) -> str:
    """Гость входит кодом; вернуть токен его сессии."""
    with tenant_context(hotel):
        grant = await guests_api.start_guest_session(
            guests_api.GuestSessionStart(
                room_number=room,
                code=code,
                identity_external_id=str(uuid.uuid4()),
                consent_version="v1",
            )
        )
    assert grant is not None
    return grant.session_token


async def test_new_booking_is_checked_in_from_pms(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Заезд (п. 1): Stay из Opera со сроком «расчётный час дня выезда», код, событие, журнал."""
    book(pms, hotel, "305", "123456", nights=2, guests=3)
    assert await sync(pms, hotel) == 1

    stay = (await active(hotel))["305"]
    assert (stay.source.value, stay.pms_booking_ref, stay.guests_count) == ("pms", "123456", 3)
    assert stay.check_out_at == noon(today() + timedelta(days=2))
    with tenant_context(hotel):
        async with session_scope() as session:
            assert await session.scalar(
                select(StayAccessCode.id).where(StayAccessCode.stay_id == stay.id)
            )
            events = list(await session.scalars(select(OutboxEvent.event_name)))
    assert "stay.checked_in" in events
    [row] = await journal(hotel)
    assert (row.action, row.actor_user_id, row.room_number) == (
        StaffAction.STAY_PMS_CHECKED_IN,
        None,
        "305",
    )


async def test_manual_stay_of_the_booking_is_adopted_and_guest_stays_signed_in(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Усыновление: ручной Stay этой брони получает её номер; код и сессия гостя живут."""
    result = await manual_check_in(hotel, "305")
    token = await bind_guest(hotel, "305", result.access_code)
    book(pms, hotel, "305", "123456", nights=5, guests=2)
    assert await sync(pms, hotel) == 1

    stay = (await active(hotel))["305"]
    assert stay.id == result.stay.id
    assert (stay.source.value, stay.pms_booking_ref, stay.guests_count) == ("pms", "123456", 2)
    assert stay.check_out_at == noon(today() + timedelta(days=5))
    with tenant_context(hotel):
        assert await guests_api.resolve_session(token) is not None
    await bind_guest(hotel, "305", result.access_code)  # прежний код тоже действует
    assert (await journal(hotel))[-1].action is StaffAction.STAY_PMS_LINKED


async def test_after_midnight_arrival_is_adopted_by_business_date(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Заехал в 01:00: в Opera `arrival` — вчера (бизнес-дата), Stay — сегодня. Тот же гость."""
    result = await manual_check_in(hotel, "305")
    book(pms, hotel, "305", "123456", arrival=today() - timedelta(days=1), nights=3)
    await sync(pms, hotel)
    assert (await active(hotel))["305"].id == result.stay.id
    assert (await active(hotel))["305"].pms_booking_ref == "123456"


async def test_manual_stay_of_another_guest_is_a_discrepancy(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Ручной Stay заехал раньше брони — другой гость: Stay не тронут, ресепшену — строка."""
    result = await manual_check_in(hotel, "305", checked_in_days_ago=2)
    book(pms, hotel, "305", "123456")
    assert await sync(pms, hotel) == 0

    stay = (await active(hotel))["305"]
    assert (stay.id, stay.source.value, stay.pms_booking_ref) == (result.stay.id, "manual", None)
    [item] = (await state(hotel)).discrepancies
    assert item == {
        "kind": "occupied_by_manual",
        "room_number": "305",
        "booking_ref": "123456",
        "other_booking_ref": None,
        "arrival": today().isoformat(),
        "manual_check_in": (today() - timedelta(days=2)).isoformat(),
        "target_room": None,
    }


async def test_booking_whose_stay_was_closed_is_not_checked_in_again(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Выезд в кабинете раньше Opera: бронь в снимке ещё есть, но Stay заново не заводится."""
    book(pms, hotel, "305", "123456")
    await sync(pms, hotel)
    with tenant_context(hotel):
        await guests_api.check_out((await active(hotel))["305"].id)

    assert await sync(pms, hotel) == 0
    assert await active(hotel) == {}
    assert (await state(hotel)).discrepancies == []


async def test_move_and_new_departure_follow_opera(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Переселение и срок (п. 2): Stay тот же, комната и дата — из Opera."""
    book(pms, hotel, "305", "123456", nights=2)
    await sync(pms, hotel)
    stay_id = (await active(hotel))["305"].id
    pms.move_booking(hotel, "123456", "410")
    pms.set_departure(hotel, "123456", today() + timedelta(days=4))
    assert await sync(pms, hotel) == 2

    stay = (await active(hotel))["410"]
    assert stay.id == stay_id
    assert stay.check_out_at == noon(today() + timedelta(days=4))
    moved = [r for r in await journal(hotel) if r.action is StaffAction.STAY_PMS_MOVED]
    assert [(r.room_number, r.room_number_to) for r in moved] == [("305", "410")]


async def test_same_departure_date_keeps_late_checkout_from_cabinet(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Поздний выезд, продлённый на стойке до 15:00: дата в Opera та же — час не трогается."""
    book(pms, hotel, "305", "123456", nights=2)
    await sync(pms, hotel)
    late = noon(today() + timedelta(days=2), hour=15)
    with tenant_context(hotel):
        await guests_api.extend_stay((await active(hotel))["305"].id, late)

    pms.set_departure(hotel, "123456", today() + timedelta(days=2))
    assert await sync(pms, hotel) == 0
    assert (await active(hotel))["305"].check_out_at == late


async def test_departure_already_past_is_not_written(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Новый срок в прошлом не правится: гость по Opera ещё в доме, выезд оформит п. 3."""
    book(pms, hotel, "305", "123456", arrival=today() - timedelta(days=3), nights=5)
    await sync(pms, hotel)
    pms.set_departure(hotel, "123456", today() - timedelta(days=1))
    assert await sync(pms, hotel) == 0
    assert (await active(hotel))["305"].check_out_at == noon(today() + timedelta(days=2))


async def test_check_out_on_second_missing_snapshot(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Выезд (п. 3): один пропуск — не повод (усечённый файл), второй подряд — выезд."""
    book(pms, hotel, "305", "123456")
    await sync(pms, hotel)
    pms.check_out_booking(hotel, "123456")

    assert await sync(pms, hotel) == 0
    assert (await active(hotel))["305"].pms_missed_snapshots == 1
    assert await sync(pms, hotel) == 1
    assert await active(hotel) == {}
    assert (await journal(hotel))[-1].action is StaffAction.STAY_PMS_CHECKED_OUT


async def test_booking_back_in_snapshot_resets_missed_counter(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    book(pms, hotel, "305", "123456")
    await sync(pms, hotel)
    pms.check_out_booking(hotel, "123456")
    await sync(pms, hotel)
    book(pms, hotel, "305", "123456")
    await sync(pms, hotel)
    assert (await active(hotel))["305"].pms_missed_snapshots == 0


async def test_manual_stay_is_never_closed_by_snapshot(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Ручной Stay (п. 4) снимок не закрывает: его ведут ресепшен и автовыезд."""
    result = await manual_check_in(hotel, "101")
    for _ in range(3):
        await sync(pms, hotel)
    assert (await active(hotel))["101"].id == result.stay.id


async def test_arrival_into_room_of_leaving_booking_waits_for_its_check_out(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Комната занята уходящей бронью: заезд ждёт её выезда без расхождения, потом — сам."""
    book(pms, hotel, "305", "111111")
    await sync(pms, hotel)
    pms.check_out_booking(hotel, "111111")
    book(pms, hotel, "305", "222222")

    assert await sync(pms, hotel) == 0
    assert (await active(hotel))["305"].pms_booking_ref == "111111"
    assert (await state(hotel)).discrepancies == []
    assert await sync(pms, hotel) == 2  # выезд первой брони и заезд второй — одним прогоном
    assert (await active(hotel))["305"].pms_booking_ref == "222222"


async def test_blocked_move_and_occupied_room_are_discrepancies(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Бронь переехала в занятую 410 — расхождение; в её прежнюю 305 заселили другую — тоже."""
    book(pms, hotel, "305", "111111")
    await sync(pms, hotel)
    await manual_check_in(hotel, "410", checked_in_days_ago=1)
    pms.move_booking(hotel, "111111", "410")
    book(pms, hotel, "305", "222222")
    assert await sync(pms, hotel) == 0

    house = await active(hotel)
    assert (house["305"].pms_booking_ref, house["410"].source.value) == ("111111", "manual")
    kinds = {
        (d["kind"], d["room_number"], d["booking_ref"]) for d in (await state(hotel)).discrepancies
    }
    assert kinds == {("move_blocked", "305", "111111"), ("occupied_by_pms", "305", "222222")}


async def test_sharers_are_one_stay_and_the_living_guest_keeps_it(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Две брони в одной комнате — один Stay: меньший номер, поздний выезд, сумма гостей;
    подселённый с меньшим номером не закрывает Stay того, кто уже живёт."""
    book(pms, hotel, "501", "200", nights=2, guests=1)
    book(pms, hotel, "501", "100", nights=4, guests=2)
    await sync(pms, hotel)
    stay = (await active(hotel))["501"]
    assert (stay.pms_booking_ref, stay.guests_count) == ("100", 3)
    assert stay.check_out_at == noon(today() + timedelta(days=4))

    book(pms, hotel, "502", "300", nights=2)
    await sync(pms, hotel)
    book(pms, hotel, "502", "050", nights=2)
    for _ in range(2):
        await sync(pms, hotel)
    assert (await active(hotel))["502"].pms_booking_ref == "300"
