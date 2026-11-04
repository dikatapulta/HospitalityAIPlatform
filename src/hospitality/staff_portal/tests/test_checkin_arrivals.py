"""«Заселены за день» на странице заселения (#832): день по поясу отеля, статусы
подключения, пустой день, чужой тенант не виден."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import checkin, checkin_arrivals
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)

CHECKIN_PAGE = f"/staff/{HOTEL_SLUG}/checkin"
ALMATY = ZoneInfo("Asia/Almaty")
# «Сейчас» кабинета в тестах границы дня: среда 28.10.2026, полдень в Алматы.
NOON = datetime(2026, 10, 28, 12, 0, tzinfo=ALMATY)


def _arrival_rows(page: str) -> list[str]:
    """Тексты строк блока «Заселены за день» без разметки, сверху вниз."""
    block = page.split('id="arrivals-title"', 1)[1].split("</section>", 1)[0]
    items = re.findall(r"<li>(.*?)</li>", block, flags=re.S)
    return [" ".join(re.sub(r"<[^>]+>", " ", item).split()) for item in items]


def _heading(page: str) -> str:
    match = re.search(r'id="arrivals-title">([^<]+)</h2>', page)
    assert match is not None
    return match.group(1)


async def _check_in_at(
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
    room: str,
    moment: datetime,
    *,
    nights: int = 2,
) -> guests_api.StayRead:
    """Stay, заселённый в `moment`: часы домена подменены только на время заселения."""
    with monkeypatch.context() as patch:
        patch.setattr("hospitality.modules.guests.service.utc_now", lambda: moment.astimezone(UTC))
        with tenant_context(portal_hotel.tenant_id):
            result = await guests_api.check_in(
                guests_api.StayCheckIn(
                    room_number=room, check_out_at=moment + timedelta(days=nights)
                )
            )
    return result.stay


async def _check_in_now(
    tenant_id: uuid.UUID, room: str, check_out_in: timedelta = timedelta(days=2)
) -> guests_api.StayRead:
    with tenant_context(tenant_id):
        result = await guests_api.check_in(
            guests_api.StayCheckIn(room_number=room, check_out_at=utc_now() + check_out_in)
        )
    return result.stay


async def _connect_guest(tenant_id: uuid.UUID, stay: guests_api.StayRead) -> None:
    """Гость открыл QR с листка — сессия Stay, как в карточке («Подключений: 1»)."""
    with tenant_context(tenant_id):
        token = await guests_api.issue_bind_link(stay.id)
        grant = await guests_api.start_guest_session_by_bind_link(
            guests_api.GuestSessionBind(
                bind_token=token, identity_external_id=str(uuid.uuid4()), consent_version="v1"
            )
        )
    assert grant is not None


def test_parse_arrivals_day_falls_back_to_today() -> None:
    today = date(2026, 11, 4)
    parse = checkin_arrivals.parse_arrivals_day
    assert parse(None, today=today) == today
    assert parse("вчера", today=today) == today
    assert parse("2026-11-05", today=today) == today  # будущего дня в списке нет
    assert parse("1999-12-31", today=today) == today
    assert parse("2026-11-03", today=today) == date(2026, 11, 3)


def test_arrivals_heading_follows_the_day() -> None:
    today = date(2026, 11, 4)
    heading = checkin_arrivals.arrivals_heading
    assert heading(today, today=today) == "Заселены сегодня"
    assert heading(date(2026, 11, 3), today=today) == "Заселены вчера"
    assert heading(date(2026, 11, 1), today=today) == "Заселены 1 ноября"
    assert heading(date(2025, 12, 30), today=today) == "Заселены 30 декабря 2025"


async def test_day_is_hotel_day_across_midnight(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """00:05 по Алматы — уже сегодня, хотя в UTC это ещё вчера; 23:55 — вчера."""
    monkeypatch.setattr(checkin, "utc_now", lambda: NOON.astimezone(UTC))
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    await _check_in_at(
        portal_hotel, monkeypatch, "1512", datetime(2026, 10, 28, 0, 5, tzinfo=ALMATY)
    )
    await _check_in_at(
        portal_hotel, monkeypatch, "305", datetime(2026, 10, 28, 9, 30, tzinfo=ALMATY)
    )
    await _check_in_at(
        portal_hotel, monkeypatch, "204", datetime(2026, 10, 27, 23, 55, tzinfo=ALMATY), nights=1
    )

    today = (await client.get(CHECKIN_PAGE)).text
    assert _heading(today) == "Заселены сегодня"
    assert _arrival_rows(today) == [
        "Комната 305 · 2 ночи · 09:30 · Ждём гостя",
        "Комната 1512 · 2 ночи · 00:05 · Ждём гостя",
    ]
    assert f'href="{CHECKIN_PAGE}?room=305"' in today

    yesterday = (await client.get(CHECKIN_PAGE, params={"day": "2026-10-27"})).text
    assert _heading(yesterday) == "Заселены вчера"
    assert _arrival_rows(yesterday) == ["Комната 204 · 1 ночь · 23:55 · Ждём гостя"]
    assert f'href="{CHECKIN_PAGE}?day=2026-10-27" aria-current="page"' in yesterday

    empty = (await client.get(CHECKIN_PAGE, params={"day": "2026-10-26"})).text
    assert _heading(empty) == "Заселены 26 октября"
    assert _arrival_rows(empty) == []
    assert checkin_arrivals.EMPTY_PAST in empty


async def test_statuses_connected_waiting_not_connected_checked_out(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await store_hotel_config(portal_hotel.tenant_id)
    await submit_login(client, portal_hotel.login)
    tenant = portal_hotel.tenant_id
    connected = await _check_in_now(tenant, "101")
    await _connect_guest(tenant, connected)
    await _check_in_now(tenant, "102")
    await _check_in_now(tenant, "103", check_out_in=-timedelta(minutes=1))  # просрочен
    left_connected = await _check_in_now(tenant, "104")
    await _connect_guest(tenant, left_connected)
    left_alone = await _check_in_now(tenant, "105")
    with tenant_context(tenant):
        await guests_api.check_out(left_connected.id)
        await guests_api.check_out(left_alone.id)

    page = (await client.get(CHECKIN_PAGE)).text
    rows = {row.split(" · ", 1)[0]: row for row in _arrival_rows(page)}

    assert rows["Комната 101"].endswith("· Гость подключился")
    assert rows["Комната 102"].endswith("· Ждём гостя")
    assert rows["Комната 103"].endswith("· Не подключился")
    # Выезд гасит сессии, но «подключался ли» после выезда остаётся правдой.
    assert rows["Комната 104"].endswith("· Гость подключился · выехал")
    assert rows["Комната 105"].endswith("· Не подключился · выехал")
    assert f'href="{CHECKIN_PAGE}?room=103"' in page
    assert f'href="{CHECKIN_PAGE}?room=104"' not in page  # у выехавшего — без действий


async def test_empty_today_explains_the_block(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)

    page = (await client.get(CHECKIN_PAGE)).text
    assert _heading(page) == "Заселены сегодня"
    assert checkin_arrivals.EMPTY_TODAY in page


async def test_other_tenant_arrivals_are_not_visible(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """P-4: заселение соседнего отеля в тот же день на странице не видно (RLS)."""
    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Other")
        session.add(other)
        await session.flush()
        other_id = other.id
    await _check_in_now(other_id, "777")
    await _check_in_now(portal_hotel.tenant_id, "101")
    await submit_login(client, portal_hotel.login)

    rows = _arrival_rows((await client.get(CHECKIN_PAGE)).text)
    assert [row.split(" · ", 1)[0] for row in rows] == ["Комната 101"]


async def test_card_page_has_no_arrivals_block(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    await _check_in_now(portal_hotel.tenant_id, "101")

    card = (await client.get(CHECKIN_PAGE, params={"room": "101"})).text
    assert 'id="arrivals-title"' not in card
    assert "page-checkin" not in card
