"""Отметка продления в Stay (spec 0062 §3, §8 PR 3, issue #668).

`extended_at`/`extended_from` пишут оба пути продления — «+N ночей» кабинета и сверка
Opera — и только когда срок вырос: ранний выезд из Opera и поздний выезд в тот же
день (`set_checkout_time`) их не трогают. `list_recent_extensions` — активные Stay,
продлённые не раньше границы, своего отеля.

Фикстуры «дома отеля» — из `test_pms_sync.py`; F811 отключён на файл: параметр-фикстура
обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import timedelta

from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.guests import api as guests_api
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from tests.test_pms_sync import (  # noqa: F401  (фикстуры для pytest)
    active,
    add_hotel,
    book,
    hotel,
    noon,
    pms,
    sync,
    today,
)


async def _manual_stay(hotel: uuid.UUID, room: str, nights: int = 1) -> guests_api.StayRead:
    with tenant_context(hotel):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=noon(today() + timedelta(nights)))
        )
    return result.stay


async def test_cabinet_extension_marks_when_and_from_what(hotel: uuid.UUID) -> None:
    stay = await _manual_stay(hotel, "305")
    before = utc_now()
    with tenant_context(hotel):
        extended = await guests_api.extend_stay(stay.id, noon(today() + timedelta(days=3)))

    assert extended.extended_from == stay.check_out_at
    assert extended.extended_at is not None and before <= extended.extended_at <= utc_now()
    assert stay.extended_at is None and stay.extended_from is None


async def test_late_checkout_same_day_is_not_an_extension(hotel: uuid.UUID) -> None:
    stay = await _manual_stay(hotel, "305")
    with tenant_context(hotel):
        late = await guests_api.set_checkout_time(stay.id, noon(today() + timedelta(1), hour=16))
    assert (late.extended_at, late.extended_from) == (None, None)


async def test_opera_extension_marks_and_early_departure_does_not(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    book(pms, hotel, "305", "123456", nights=2)
    await sync(pms, hotel)

    pms.set_departure(hotel, "123456", today() + timedelta(days=4))
    assert await sync(pms, hotel) == 1
    extended = (await active(hotel))["305"]
    assert extended.extended_from == noon(today() + timedelta(days=2))
    marked_at = extended.extended_at
    assert marked_at is not None

    pms.set_departure(hotel, "123456", today() + timedelta(days=3))
    assert await sync(pms, hotel) == 1
    shortened = (await active(hotel))["305"]
    assert shortened.check_out_at == noon(today() + timedelta(days=3))
    assert (shortened.extended_at, shortened.extended_from) == (
        marked_at,
        noon(today() + timedelta(days=2)),
    )


async def test_recent_extensions_are_active_since_and_own_hotel(hotel: uuid.UUID) -> None:
    other = await add_hotel("hotel-other")
    old = await _manual_stay(hotel, "301")
    fresh = await _manual_stay(hotel, "302")
    left = await _manual_stay(hotel, "303")
    await _manual_stay(hotel, "304")  # не продлевали
    foreign = await _manual_stay(other, "302")
    later = noon(today() + timedelta(days=3))
    with tenant_context(hotel):
        await guests_api.extend_stay(old.id, later)
    boundary = utc_now()
    with tenant_context(hotel):
        await guests_api.extend_stay(fresh.id, later)
        await guests_api.extend_stay(left.id, later)
        await guests_api.check_out(left.id)
    with tenant_context(other):
        await guests_api.extend_stay(foreign.id, later)

    with tenant_context(hotel):
        recent = await guests_api.list_recent_extensions(boundary)
    assert [(stay.id, stay.room_number) for stay in recent] == [(fresh.id, "302")]
