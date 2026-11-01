"""Главная кабинета: плитки разделов с живыми числами (issue #454).

Что проверяется: видимость плиток по ролям (мини-матрица docs/RBAC.md),
числа очереди — новых / в работе / просрочено тем же `queue.is_overdue`, что у
карточки и сигнала, — числа заселения по местному дню отеля (граница — полночь
Алматы, а не UTC), приветствие по часам отеля и главная отеля без конфига.
Часы двигаются подменой `home.utc_now` — канон тестов очереди: в проде стареет
заявка, а не подделывается её `created_at`.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import checkin, home
from hospitality.staff_portal.tests.conftest import (
    HOTEL_SLUG,
    PortalHotel,
    store_hotel_config,
    submit_login,
)
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
HOTEL_ZONE = ZoneInfo("Asia/Almaty")


async def _make_request(tenant_id: uuid.UUID, summary: str) -> requests_api.ServiceRequestRead:
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get("housekeeping") or await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )
        return await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
                summary=summary,
                room_number="305",
            )
        )


async def _check_in(tenant_id: uuid.UUID, room: str) -> guests_api.StayRead:
    """Заселение на одну ночь: выезд — завтра 12:00 по Алматы (правило кабинета)."""
    with tenant_context(tenant_id):
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=room,
                check_out_at=checkin.checkout_at_utc(
                    checkin.HotelTiming(zone=HOTEL_ZONE), 1, now=utc_now()
                ),
            )
        )
    return checked.stay


async def _login_as(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    role: StaffRole,
    service_keys: tuple[str, ...] = (),
) -> None:
    staff_login = unique_login()
    await create_staff_user(
        staff_login,
        tenant_id=portal_hotel.tenant_id,
        role=role,
        service_keys=service_keys,
        display_name="Санжар Нурланов",
    )
    await submit_login(client, staff_login)


def _metric(html: str, key: str) -> int:
    match = re.search(rf'data-metric="{key}">(\d+)<', html)
    assert match is not None, f"no metric {key}"
    return int(match.group(1))


def _chip(html: str, tone: str) -> int | None:
    match = re.search(rf'data-chip="{tone}">.*?<b>(\d+)</b>', html)
    return int(match.group(1)) if match else None


def _local_midnight_after(moment: datetime) -> datetime:
    local = moment.astimezone(HOTEL_ZONE)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), HOTEL_ZONE)
    return midnight.astimezone(UTC)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (4, "Доброй ночи"),
        (5, "Доброе утро"),
        (11, "Доброе утро"),
        (12, "Добрый день"),
        (17, "Добрый день"),
        (18, "Добрый вечер"),
        (22, "Добрый вечер"),
        (23, "Доброй ночи"),
        (None, "Здравствуйте"),
    ],
)
def test_greeting_follows_the_hotel_hour(hour: int | None, expected: str) -> None:
    assert home.greeting(hour) == expected


def test_date_label_and_first_name() -> None:
    assert home.date_label(date(2026, 10, 19)) == "Понедельник, 19 октября"
    assert home.date_label(date(2026, 10, 25)) == "Воскресенье, 25 октября"
    assert home.first_name("Аружан Менеджер") == "Аружан"
    assert home.first_name("Санжар") == "Санжар"


async def test_staff_role_sees_only_the_queue_tile(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Горничная и техник — только очередь: заселение им закрыто, и страница
    не показывает (и не считает) его чисел."""
    await _login_as(client, portal_hotel, StaffRole.STAFF, ("housekeeping",))
    response = await client.get(HOME)
    assert response.status_code == 200
    assert 'data-tile="queue"' in response.text
    assert 'data-tile="checkin"' not in response.text
    assert 'data-tile="turnovers"' not in response.text  # доска выключена (spec 0046 §5)
    for hidden in ("Сводка дня", "Справочник отеля", "Журнал изменений", "Сотрудники"):
        assert hidden not in response.text


async def test_receptionist_sees_queue_and_checkin_but_no_manager_sections(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)
    response = await client.get(HOME)
    assert 'data-tile="queue"' in response.text
    assert 'data-tile="checkin"' in response.text
    for hidden in ("Сводка дня", "Справочник отеля", "Журнал изменений", "Сотрудники"):
        assert hidden not in response.text


async def test_manager_sees_every_section_with_links(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    for path in ("requests", "checkin", "summary", "knowledge", "changes", "team"):
        assert f'href="{HOME}/{path}"' in response.text
    assert "Аружан Менеджер" in response.text  # карточка меню: кто вошёл


async def test_queue_numbers_new_in_progress_and_overdue(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новых / в работе / просрочено — то же, что увидит открывший очередь.

    «Просрочено» появляется только когда оно есть, и считается тем же
    `queue.is_overdue`: взятая заявка не просрочена, сколько бы ни висела.
    """
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await _make_request(portal_hotel.tenant_id, "полотенца")
    taken = await _make_request(portal_hotel.tenant_id, "лампа")
    done = await _make_request(portal_hotel.tenant_id, "вода")
    with tenant_context(portal_hotel.tenant_id):
        for request, status in (
            (taken, requests_api.RequestStatus.IN_PROGRESS),
            (done, requests_api.RequestStatus.IN_PROGRESS),
            (done, requests_api.RequestStatus.DONE),
        ):
            await requests_api.change_request_status(request.id, status)
    await submit_login(client, portal_hotel.login)

    fresh = (await client.get(HOME)).text
    assert _metric(fresh, "queue") == 3
    assert _chip(fresh, "new") == 2
    assert _chip(fresh, "progress") == 1
    assert _chip(fresh, "overdue") is None
    assert "home-tile-alert" not in fresh

    monkeypatch.setattr(home, "utc_now", lambda: utc_now() + timedelta(minutes=31))
    later = (await client.get(HOME)).text
    assert _chip(later, "overdue") == 2
    assert "Просрочено" in later
    assert "home-tile-alert" in later

    # «Требует внимания» (#462): просроченные — первыми, старшая сверху, взятая
    # — после них; закрытой нет.
    attention = re.search(r"<ul class=\"home-queue\" data-attention>.*?</ul>", later, re.S)
    assert attention is not None
    rows = re.findall(
        r'home-queue-title">([^<]+)<.*?home-status-(\w+)">([^<]+)<', attention.group(0), re.S
    )
    assert rows == [
        ("убрать 305", "overdue", "Просрочена"),
        ("полотенца", "overdue", "Просрочена"),
        ("лампа", "progress", "В работе"),
    ]
    assert "вода" not in attention.group(0)


async def test_stalled_request_leads_attention_but_not_overdue_count(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Долго в работе» (spec 0028 §8) — первой в «Требует внимания», но в
    плитку «просрочено» не идёт: там только невзятые, как в очереди."""
    await store_hotel_config(
        portal_hotel.tenant_id, reminder_after_minutes=None, stalled_after_minutes=60
    )
    await _make_request(portal_hotel.tenant_id, "полотенца")
    taken = await _make_request(portal_hotel.tenant_id, "кондиционер")
    with tenant_context(portal_hotel.tenant_id):
        await requests_api.change_request_status(taken.id, requests_api.RequestStatus.IN_PROGRESS)
    await submit_login(client, portal_hotel.login)

    monkeypatch.setattr(home, "utc_now", lambda: utc_now() + timedelta(minutes=61))
    page = (await client.get(HOME)).text
    assert _chip(page, "overdue") is None
    attention = re.search(r"<ul class=\"home-queue\" data-attention>.*?</ul>", page, re.S)
    assert attention is not None
    rows = re.findall(
        r'home-queue-title">([^<]+)<.*?home-status-(\w+)">([^<]+)<', attention.group(0), re.S
    )
    assert rows == [
        ("кондиционер", "overdue", "Долго в работе"),
        ("полотенца", "new", "Новая"),
    ]


async def test_checkin_numbers_follow_the_hotel_local_day(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Заселено сегодня» и «выезжают сегодня» — по полуночи Алматы, не UTC.

    Заселили сейчас, выезд — завтра 12:00 по отелю. За секунду до местной
    полуночи заселение ещё «сегодня», выезд — ещё нет; в полночь наоборот.
    """
    await store_hotel_config(portal_hotel.tenant_id)
    stay = await _check_in(portal_hotel.tenant_id, "101")
    await _check_in(portal_hotel.tenant_id, "102")
    midnight = _local_midnight_after(stay.check_in_at)
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    monkeypatch.setattr(home, "utc_now", lambda: midnight - timedelta(seconds=1))
    before = (await client.get(HOME)).text
    assert _metric(before, "checkin") == 2
    assert _chip(before, "muted") == 0

    monkeypatch.setattr(home, "utc_now", lambda: midnight)
    after = (await client.get(HOME)).text
    assert _metric(after, "checkin") == 0
    assert _chip(after, "muted") == 2

    with tenant_context(portal_hotel.tenant_id):
        await guests_api.check_out(stay.id)
    checked_out = (await client.get(HOME)).text
    assert _chip(checked_out, "muted") == 1  # выехавший уже не «выезжает»


async def test_greeting_and_date_use_the_hotel_timezone(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """19.10 23:30 UTC — в Алматы (UTC+5) уже 20.10, 4:30 утра: «Доброй ночи»
    и вторник, а не «Добрый вечер» и понедельник по UTC."""
    await store_hotel_config(portal_hotel.tenant_id)
    monkeypatch.setattr(home, "utc_now", lambda: datetime(2026, 10, 19, 23, 30, tzinfo=UTC))
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    assert "Вторник, 20 октября" in response.text
    assert "Доброй ночи, Аружан" in response.text


async def test_home_opens_for_a_hotel_without_config(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """До онбординга конфига нет: главная открывается, приветствие — без времени
    суток и без даты (часов отеля мы не знаем), просрочки нет по определению."""
    await _make_request(portal_hotel.tenant_id, "убрать 305")
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    assert response.status_code == 200
    assert "Здравствуйте, Аружан" in response.text
    assert "home-date" not in response.text
    assert _metric(response.text, "queue") == 1
    assert _chip(response.text, "overdue") is None
    assert _metric(response.text, "checkin") == 0


async def test_home_keeps_the_signal(client: AsyncClient, portal_hotel: PortalHotel) -> None:
    """Сигнал кабинета звучит и на главной (spec 0039 §3.1) — новая раскладка
    его не потеряла; стили главной отдаются как статика кабинета."""
    await submit_login(client, portal_hotel.login)
    response = await client.get(HOME)
    assert 'id="signal"' in response.text
    assert "/staff/static/signal.js" in response.text
    stylesheet = re.search(r'href="(/staff/static/home\.css\?v=\w+)"', response.text)
    assert stylesheet is not None
    assert (await client.get(stylesheet.group(1))).status_code == 200
    assert "style=" not in response.text  # CSP: инлайн-стили не пропускаются
