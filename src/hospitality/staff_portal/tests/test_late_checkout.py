"""Поздний выезд в кабинете (spec 0060 §2.2–§2.4, §4 п. 1, 5, 7, 8; issue #667).

Карточка проживания: кнопки по расчётному часу, «Другой час…», значок и строка
при позднем выезде, «Вернуть выезд в 12:00»; действие `late-checkout` и его
отказы; «+1 ночь» снимает поздний выезд; строка журнала «Время выезда»; права
как у «Продлить». Доска «Выезды и уборка»: блок «Поздний выезд сегодня».
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from hospitality.modules.guests import api as guests_api
from hospitality.platform.models import StaffAction, StaffActionLog, StaffRole, Tenant
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import checkin, turnovers
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import create_staff_user, unique_login

SAME_ORIGIN = {"origin": "https://test"}
CHECKIN_PAGE = f"/staff/{HOTEL_SLUG}/checkin"
STAYS_API = f"/staff/{HOTEL_SLUG}/api/stays"
BOARD_FRAGMENT = f"/staff/{HOTEL_SLUG}/turnovers/fragment"
ALMATY = ZoneInfo("Asia/Almaty")
NOTE_16 = "Поздний выезд до 16:00 — чат гостя работает до этого часа."

# Заголовок блока доски и его число (_turnover_board.html).
_LATE_TITLE = re.compile(r'Поздний выезд сегодня — <span class="num">(\d+)</span>')
_LATE_ROW = re.compile(r'<li class="num">([^<]+)</li>')


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=ALMATY).astimezone(UTC)


async def _stay(tenant_id: uuid.UUID, room: str, check_out_at: datetime) -> guests_api.StayRead:
    with tenant_context(tenant_id):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=check_out_at)
        )
    return result.stay


async def _late(client: AsyncClient, stay_id: uuid.UUID, hour: object) -> dict[str, object]:
    response = await client.post(
        f"{STAYS_API}/{stay_id}/late-checkout", json={"hour": hour}, headers=SAME_ORIGIN
    )
    body: dict[str, object] = response.json()
    body["_status"] = response.status_code
    return body


def test_late_checkout_hours_follow_checkout_hour() -> None:
    """Кнопки — расчётный + 2, 4, 6 ч, «Другой час…» — расчётный + 1 … 23:00;
    позже 23:00 часа нет (§2.2)."""

    def hours(checkout_hour: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
        timing = checkin.HotelTiming(zone=ALMATY, checkout_hour=checkout_hour)
        return checkin.late_checkout_hours(timing)

    assert hours(12) == ((14, 16, 18), tuple(range(13, 24)))
    assert hours(18) == ((20, 22), tuple(range(19, 24)))
    assert hours(22) == ((), (23,))
    assert hours(23) == ((), ())


def test_extended_checkout_puts_checkout_hour_not_late_hour() -> None:
    """«+1 ночь» Stay с поздним выездом до 16:00 — завтра в расчётный час (§2.1);
    просроченному — от сегодня."""
    timing = checkin.HotelTiming(zone=ALMATY, checkout_hour=12)
    day = date(2026, 11, 3)
    now = datetime(2026, 11, 3, 13, 0, tzinfo=ALMATY)
    assert checkin.extended_checkout(_at(day, 16), timing, 1, now=now) == _at(
        day.replace(day=4), 12
    )
    overdue = _at(date(2026, 10, 30), 12)
    assert checkin.extended_checkout(overdue, timing, 1, now=now) == _at(day.replace(day=4), 12)


def test_is_late_checkout_compares_local_clock_with_checkout_hour() -> None:
    timing = checkin.HotelTiming(zone=ALMATY, checkout_hour=12)
    day = date(2026, 11, 3)

    def stay(moment: datetime) -> guests_api.StayRead:
        return guests_api.StayRead(
            id=uuid.uuid4(),
            guest_id=uuid.uuid4(),
            room_number="704",
            status=guests_api.StayStatus.CHECKED_IN,
            guests_count=1,
            check_in_at=moment - timedelta(days=1),
            check_out_at=moment,
        )

    assert not checkin.is_late_checkout(stay(_at(day, 12)), timing)
    assert not checkin.is_late_checkout(stay(_at(day, 11)), timing)
    assert checkin.is_late_checkout(stay(_at(day, 16)), timing)
    assert checkin.late_checkout_note(stay(_at(day, 16)), timing) == NOTE_16
    assert checkin.late_checkout_note(stay(_at(day, 12)), timing) is None


async def test_late_checkout_sets_hour_on_departure_day_and_back(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """16:00 в день выезда по поясу отеля → UTC, строка журнала «Время выезда»;
    «Вернуть выезд в 12:00» — тот же путь (§4 п. 1)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    departure = utc_now().astimezone(ALMATY).date() + timedelta(days=2)
    stay = await _stay(portal_hotel.tenant_id, "704", _at(departure, 12))

    late = await _late(client, stay.id, 16)
    assert late["_status"] == 200
    assert late["check_out_local"] == f"{departure:%d.%m.%Y} 16:00"
    assert late["late_checkout_note"] == NOTE_16
    with tenant_context(portal_hotel.tenant_id):
        stored = await guests_api.get_active_stay(stay.id)
    assert stored is not None and stored.check_out_at == _at(departure, 16)

    back = await _late(client, stay.id, 12)
    assert back["_status"] == 200
    assert back["check_out_local"] == f"{departure:%d.%m.%Y} 12:00"
    assert back["late_checkout_note"] is None

    with tenant_context(portal_hotel.tenant_id):
        async with session_scope() as session:
            rows = list(await session.scalars(select(StaffActionLog)))
    changed = [row for row in rows if row.action is StaffAction.STAY_CHECKOUT_TIME_CHANGED]
    assert [(row.room_number, row.actor_user_id) for row in changed] == [
        ("704", portal_hotel.user_id),
        ("704", portal_hotel.user_id),
    ]
    changes = (await client.get(f"/staff/{HOTEL_SLUG}/changes")).text
    assert "Время выезда" in changes


async def test_late_checkout_rejects_early_hour_bad_hour_and_past(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Раньше расчётного и вне 0–23 — 422 ERR-PLATFORM-002; срок в прошлом —
    ERR-GUESTS-004, текст карточки — «Это время уже прошло…» (§2.2)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    today = utc_now().astimezone(ALMATY).date()
    stay = await _stay(portal_hotel.tenant_id, "704", _at(today + timedelta(days=1), 12))
    for hour in (11, 24, -1, "16:30"):
        rejected = await _late(client, stay.id, hour)
        assert rejected["_status"] == 422, hour
        assert rejected["error"]["code"] == "ERR-PLATFORM-002", hour  # type: ignore[index]

    long_overdue = await _stay(portal_hotel.tenant_id, "305", _at(today - timedelta(days=3), 12))
    past = await _late(client, long_overdue.id, 23)
    assert past["_status"] == 422
    assert past["error"]["code"] == "ERR-GUESTS-004"  # type: ignore[index]


async def test_extend_resets_late_checkout(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """«+1 ночь» Stay с поздним выездом — следующий день в расчётный час (§4 п. 5)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    departure = utc_now().astimezone(ALMATY).date() + timedelta(days=1)
    stay = await _stay(portal_hotel.tenant_id, "704", _at(departure, 12))
    assert (await _late(client, stay.id, 16))["_status"] == 200

    extended = await client.post(
        f"{STAYS_API}/{stay.id}/extend", json={"nights": 1}, headers=SAME_ORIGIN
    )
    assert extended.status_code == 200
    next_day = departure + timedelta(days=1)
    assert extended.json()["check_out_local"] == f"{next_day:%d.%m.%Y} 12:00"
    assert extended.json()["late_checkout_note"] is None


async def test_card_shows_late_checkout_block_and_state(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Кнопки по расчётному часу, «Другой час…»; поставлен — значок, строка и
    «Вернуть выезд в 12:00» видны (§4 п. 7)."""
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    departure = utc_now().astimezone(ALMATY).date() + timedelta(days=1)
    stay = await _stay(portal_hotel.tenant_id, "704", _at(departure, 12))

    card = (await client.get(CHECKIN_PAGE, params={"room": "704"})).text
    for hour in (14, 16, 18):
        assert f'data-hour="{hour}">до {hour}:00</button>' in card
    assert 'data-hour="20"' not in card
    assert '<option value="13">13:00</option>' in card
    assert '<option value="23">23:00</option>' in card
    assert '<span class="badge badge-late" data-late hidden>' in card
    assert NOTE_16 not in card

    assert (await _late(client, stay.id, 16))["_status"] == 200
    card = (await client.get(CHECKIN_PAGE, params={"room": "704"})).text
    assert '<span class="badge badge-late" data-late>поздний выезд</span>' in card
    assert f"{departure:%d.%m.%Y} 16:00" in card
    assert f'<p class="hint" data-late data-late-note>{NOTE_16}</p>' in card
    assert ">Вернуть выезд в 12:00</button>" in card


async def test_late_checkout_is_reception_right(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Права — как у «Продлить»: хаускипинг получает 403 (§2.2, RBAC §2)."""
    await submit_login(client, portal_hotel.login)
    departure = utc_now().astimezone(ALMATY).date() + timedelta(days=1)
    stay = await _stay(portal_hotel.tenant_id, "704", _at(departure, 12))
    maid = unique_login()
    await create_staff_user(
        maid,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.SENIOR,
        service_keys=(HOUSEKEEPING_SERVICE_KEY,),
    )
    await submit_login(client, maid)
    forbidden = await _late(client, stay.id, 16)
    assert forbidden["_status"] == 403
    assert forbidden["error"]["code"] == "ERR-AUTH-003"  # type: ignore[index]


@pytest.fixture
def _board_enabled() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


@pytest.mark.usefixtures("_board_enabled")
async def test_board_lists_todays_late_checkouts_by_time(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блок — по времени, затем по номеру; обычный выезд в 12:00, завтрашний
    поздний и чужой отель — не в нём (§4 п. 8)."""
    day = date(2026, 10, 30)
    now = _at(day, 10)
    monkeypatch.setattr(turnovers, "utc_now", lambda: now)
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    assert _LATE_TITLE.search((await client.get(BOARD_FRAGMENT)).text) is None

    for room, moment in (
        ("704", _at(day, 16)),
        ("305", _at(day, 14)),
        ("203", _at(day, 16)),
        ("101", _at(day, 12)),
        ("909", _at(day + timedelta(days=1), 16)),
    ):
        await _stay(portal_hotel.tenant_id, room, moment)
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other Hotel")
        session.add(other)
        await session.flush()
        other_id = other.id
    await _stay(other_id, "505", _at(day, 15))

    board = (await client.get(BOARD_FRAGMENT)).text
    title = _LATE_TITLE.search(board)
    assert title is not None and title.group(1) == "3"
    assert _LATE_ROW.findall(board) == ["305 · до 14:00", "203 · до 16:00", "704 · до 16:00"]
    assert turnovers.LATE_CHECKOUT_HINT in board


async def test_board_off_has_no_late_block(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Под выключателем доски (по умолчанию выключена): ни доски, ни блока (§2.3)."""
    get_settings.cache_clear()
    await submit_login(client, portal_hotel.login)
    today = utc_now().astimezone(ALMATY).date()
    await _stay(portal_hotel.tenant_id, "704", _at(today, 23))
    response = await client.get(BOARD_FRAGMENT)
    assert response.status_code == 404
    assert "Поздний выезд сегодня" not in response.text
