"""Просроченные проживания в кабинете (spec 0043 §2.5, issue #225): список
«Не выехали в срок» на странице заселения и строка автовыезда на карточке."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import checkin
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)

CHECKIN_PAGE = f"/staff/{HOTEL_SLUG}/checkin"
ALMATY = ZoneInfo("Asia/Almaty")


async def _stay_due(portal_hotel: PortalHotel, room: str, delta: timedelta) -> None:
    """Stay комнаты со сроком выезда `utc_now() + delta` (отрицательная — просрочен)."""
    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + delta)
        )


def _stay(check_out_at: datetime) -> guests_api.StayRead:
    return guests_api.StayRead(
        id=uuid.uuid4(),
        guest_id=uuid.uuid4(),
        room_number="305",
        status=guests_api.StayStatus.CHECKED_IN,
        guests_count=1,
        check_in_at=check_out_at - timedelta(days=1),
        check_out_at=check_out_at,
    )


def test_auto_checkout_label_by_hotel_clock() -> None:
    """Время — по поясу отеля; не сегодня — с датой; прошло — «в ближайшие минуты»."""
    due = datetime(2026, 10, 21, 12, 0, tzinfo=ALMATY).astimezone(UTC)
    timing = checkin.HotelTiming(zone=ALMATY, auto_checkout_grace=timedelta(hours=2))
    now = datetime(2026, 10, 21, 12, 30, tzinfo=ALMATY)

    assert checkin.auto_checkout_label(_stay(due), timing, now) == "в 14:00"
    assert (
        checkin.auto_checkout_label(_stay(due), timing, now + timedelta(hours=2))
        == "в ближайшие минуты"
    )
    long_grace = checkin.HotelTiming(zone=ALMATY, auto_checkout_grace=timedelta(hours=26))
    assert checkin.auto_checkout_label(_stay(due), long_grace, now) == "22.10 в 14:00"
    assert checkin.auto_checkout_label(_stay(due), checkin.HotelTiming(zone=ALMATY), now) is None

    [row] = checkin.overdue_rows([_stay(due)], timing, now)
    assert row == {"room_number": "305", "was_due": "в 12:00", "auto_checkout": "в 14:00"}


async def test_checkin_page_lists_overdue_stays_oldest_first(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    await _stay_due(portal_hotel, "201", -timedelta(minutes=30))
    await _stay_due(portal_hotel, "305", -timedelta(hours=1))
    await _stay_due(portal_hotel, "102", timedelta(days=1))

    page = (await client.get(CHECKIN_PAGE)).text

    assert "Не выехали в срок — 2" in page
    assert page.index("Комната 305") < page.index("Комната 201")
    assert "Комната 102" not in page
    assert "закроется сам в" in page
    assert f'href="{CHECKIN_PAGE}?room=305"' in page


async def test_checkin_page_without_overdue_has_no_block(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    await _stay_due(portal_hotel, "102", timedelta(days=1))

    assert "Не выехали в срок" not in (await client.get(CHECKIN_PAGE)).text


async def test_overdue_without_hotel_config_has_no_auto_checkout_promise(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Тенант без конфига джобой не обходится — обещать «закроется сам» нельзя."""
    await submit_login(client, portal_hotel.login)
    await _stay_due(portal_hotel, "305", -timedelta(hours=1))

    page = (await client.get(CHECKIN_PAGE)).text
    assert "Не выехали в срок — 1" in page
    assert "закроется сам" not in page


async def test_overdue_card_says_when_it_closes_itself(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    await _stay_due(portal_hotel, "305", -timedelta(hours=1))

    card = (await client.get(CHECKIN_PAGE, params={"room": "305"})).text
    assert "просрочен" in card
    assert "Если не продлить, выезд оформится сам в" in card
    assert "Не выехали в срок" not in card  # список — только на странице без карточки
