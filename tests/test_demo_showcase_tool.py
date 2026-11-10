"""Демо-картина тестового отеля на staging (#605, `tools/demo_showcase`).

Картина целиком, повтор без дублей, `--reset` и смена дня, чужие строки, вход
приглашениями без паролей, этажи вошедшим горничным и отказ вне staging — в том
числе на копии отеля, где в `.env` забыли `SENTRY_ENVIRONMENT=pilot` (#389). Под экраны
#752: «Отправленные» уборки, строки «Статистики» и брони столика в трёх стадиях.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.platform.config import LocalizedText, load_tenant_config, mutate_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import find_hotel
from hospitality.platform.staff_invites import accept_invite, list_pending_invites
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY, RECEPTION_SERVICE_KEY
from hospitality.platform.staff_team import list_tenant_members
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showcase import SHOW_DAY_HINT, build_showcase, main, run_showcase
from hospitality.tools.demo_showcase_board import (
    DEMO_REQUESTS,
    DEMO_STAYS,
    DEMO_TURNOVERS,
    check_out_moment,
)
from hospitality.tools.demo_showcase_bookings import DEMO_BOOKING_ROOM, DEMO_BOOKINGS, pick_slot
from hospitality.tools.demo_showcase_staff import DEMO_STAFF, DemoShowcaseError
from hospitality.tools.seed import seed_demo_data
from tests.test_staff_auth import PASSWORD, create_staff_user

TurnoverStatus = housekeeping_api.TurnoverStatus
RequestStatus = requests_api.RequestStatus
BookingStage = table_booking_api.BookingStage
ZONE = ZoneInfo("Asia/Almaty")
DEMO_LOGINS = {demo.login for demo in DEMO_STAFF}
STAGING_ENV = {
    "SENTRY_ENVIRONMENT": "staging",
    "TELEGRAM_TENANT_SLUG": "demo-hotel",
    "PUBLIC_BASE_URL": "https://staging.example.test",
    "ENABLE_HOUSEKEEPING_FLOORS": "true",
    "ENABLE_TURNOVER_BOARD": "true",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
async def demo_tenant(canonical_database: None, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Демо-отель сида с одним менеджером и окружение staging."""
    _set_env(monkeypatch, STAGING_ENV)
    await seed_demo_data()
    hotel = await find_hotel("demo-hotel")
    assert hotel is not None
    await create_staff_user(
        "BOSS", tenant_id=hotel.tenant_id, role=StaffRole.MANAGER, display_name="Менеджер Демо"
    )
    return hotel.tenant_id


def _midnight() -> datetime:
    return datetime.combine(utc_now().astimezone(ZONE).date(), time(0), tzinfo=ZONE).astimezone(UTC)


@dataclass(frozen=True)
class Picture:
    """Снимок демо-отеля: id и статусы того, что видно на доске и в заявках."""

    stays: dict[str, uuid.UUID]
    open_paths: dict[str, tuple[uuid.UUID, TurnoverStatus]]
    ready: list[tuple[str, uuid.UUID]]
    requests: dict[uuid.UUID, RequestStatus]  # заявки картины, без запросов на столик
    bookings: dict[uuid.UUID, RequestStatus]  # заявки броней столика
    invites: list[str]


async def _picture(tenant_id: uuid.UUID) -> Picture:
    with tenant_context(tenant_id):
        stays = await guests_api.list_active_stays()
        open_paths = await housekeeping_api.list_open_turnovers(limit=100)
        ready = await housekeeping_api.list_turnovers_ready_since(_midnight(), limit=100)
        requests = await requests_api.list_requests_created_since(
            created_after=_midnight() - timedelta(days=1), limit=100
        )
        bookings = {
            request.id
            for request in requests
            if await table_booking_api.get_booking_by_request(request.id) is not None
        }
    return Picture(
        stays={stay.room_number: stay.id for stay in stays},
        open_paths={path.room_number: (path.id, path.status) for path in open_paths},
        ready=sorted((path.room_number, path.id) for path in ready),
        requests={r.id: r.status for r in requests if r.id not in bookings},
        bookings={r.id: r.status for r in requests if r.id in bookings},
        invites=sorted(invite.login for invite in await list_pending_invites(tenant_id)),
    )


async def _stages(tenant_id: uuid.UUID) -> Counter[BookingStage]:
    """Стадии броней у проживания номера картины — то, что гость видит в «Ваших бронях»."""
    with tenant_context(tenant_id):
        stay = await guests_api.find_active_stay(DEMO_BOOKING_ROOM)
        assert stay is not None
        return Counter(b.stage for b in await table_booking_api.list_bookings_for_stay(stay.id))


_THREE_STAGES = Counter([BookingStage.WAITING, BookingStage.CONFIRMED, BookingStage.NO_SEATS])


def _invite_tokens(lines: list[str]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for line in lines:
        match = re.search(r"логин (\w+): \S+/staff/invite/(\S+)$", line)
        if match:
            tokens[match.group(1)] = match.group(2)
    return tokens


async def test_builds_full_picture_with_invites_and_no_passwords(demo_tenant: uuid.UUID) -> None:
    lines = await run_showcase(confirmed=True)

    picture = await _picture(demo_tenant)
    assert set(picture.stays) == {spec.room for spec in DEMO_STAYS}
    with tenant_context(demo_tenant):
        open_paths = await housekeeping_api.list_open_turnovers(limit=100)
        ready = await housekeeping_api.list_turnovers_ready_since(_midnight(), limit=100)
        requests = await requests_api.list_requests_created_since(
            created_after=_midnight(), limit=100
        )
        active = await guests_api.list_active_stays()
    statuses = Counter(path.status for path in [*open_paths, *ready])
    assert statuses == Counter(spec.status for spec in DEMO_TURNOVERS)
    assert len(statuses) == 5  # все шаги доски: отмечен, проверен, уборка, убран, готов
    noted = {path.room_number: path.inspection_note for path in open_paths}
    assert noted["1502"] == "Нет полотенца"
    ours = {(spec.room, spec.summary) for spec in DEMO_REQUESTS}
    assert Counter(r.status for r in requests if (r.room_number, r.summary) in ours) == Counter(
        spec.status for spec in DEMO_REQUESTS
    )
    assert await _stages(demo_tenant) == _THREE_STAGES
    assert len(DEMO_BOOKINGS) == 3
    cancelled = next(r for r in requests if r.status is RequestStatus.CANCELLED)
    assert cancelled.cancel_reason is requests_api.CancelReason.UNAVAILABLE
    leaving_today = [
        s
        for s in active
        if s.check_out_at.astimezone(ZONE).date() == utc_now().astimezone(ZONE).date()
    ]
    assert len(leaving_today) >= sum(1 for spec in DEMO_STAYS if spec.nights == 0)

    invites = {invite.login: invite for invite in await list_pending_invites(demo_tenant)}
    assert set(invites) == DEMO_LOGINS
    assert invites["DEMOREC"].service_keys == [RECEPTION_SERVICE_KEY]
    assert invites["DEMOSENIOR"].role_key is StaffRole.SENIOR
    assert invites["DEMO15"].service_keys == [HOUSEKEEPING_SERVICE_KEY]
    assert set(_invite_tokens(lines)) == DEMO_LOGINS
    # Паролей команда не заводит: учёток с демо-логинами нет, пока ссылку не примут.
    members = await list_tenant_members(demo_tenant)
    assert not {member.login for member in members} & DEMO_LOGINS
    assert not any("парол" in line.lower() for line in lines)
    assert lines[-1] == SHOW_DAY_HINT

    async with platform_session_scope() as session:
        config = await load_tenant_config(session, demo_tenant)
    assert config.request_reminder_minutes_by_category == {
        "maintenance": 10,
        "housekeeping": 180,
        "fnb": 180,
    }


async def test_rerun_same_day_keeps_the_same_picture(demo_tenant: uuid.UUID) -> None:
    await run_showcase(confirmed=True)
    first = await _picture(demo_tenant)

    lines = await run_showcase(confirmed=True)

    assert await _picture(demo_tenant) == first
    assert any("заселено сейчас — 0" in line for line in lines)
    assert any("создано сейчас — 0" in line and "Заявки" in line for line in lines)
    assert any("создано сейчас — 0" in line and "Брони столика" in line for line in lines)
    assert await _stages(demo_tenant) == _THREE_STAGES
    # Ожидающие ссылки перевыпущены, а не размножены: по одной на логин.
    assert len(await list_pending_invites(demo_tenant)) == len(DEMO_STAFF)


async def test_reset_rebuilds_and_leaves_foreign_rows(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant):
        foreign_stay = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number="101", check_out_at=utc_now() + timedelta(days=1), guests_count=1
            )
        )
        housekeeping = next(
            c for c in await requests_api.list_categories() if c.key == HOUSEKEEPING_SERVICE_KEY
        )
        foreign_request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=housekeeping.id,
                origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL,
                summary="Принести два полотенца и тапочки",
                room_number="101",
            )
        )
        foreign_path = await housekeeping_api.report_checkout("102")
    await run_showcase(confirmed=True)
    before = await _picture(demo_tenant)

    await run_showcase(confirmed=True, reset=True)

    after = await _picture(demo_tenant)
    assert after.stays["101"] == foreign_stay.stay.id
    assert all(after.stays[s.room] != before.stays[s.room] for s in DEMO_STAYS)
    assert after.open_paths["102"] == (foreign_path.turnover.id, TurnoverStatus.REPORTED)
    ours = [room for room in before.open_paths if room != "102"]
    assert all(after.open_paths[room] != before.open_paths[room] for room in ours)
    assert after.ready == before.ready  # принятый сегодня номер второй раз не создаётся
    assert after.requests[foreign_request.id] is RequestStatus.NEW
    open_kinds = {RequestStatus.NEW, RequestStatus.IN_PROGRESS}
    old_open = {rid for rid, status in before.requests.items() if status in open_kinds} - {
        foreign_request.id
    }
    assert old_open
    assert all(after.requests[rid] is RequestStatus.CANCELLED for rid in old_open)
    created_by_reset = [rid for rid in after.requests if rid not in before.requests]
    assert len(created_by_reset) == sum(1 for spec in DEMO_REQUESTS if spec.status in open_kinds)
    # Закрытые сегодня (выполнена, отменена с причиной) заново не создаются.
    assert all(after.requests[rid] in open_kinds for rid in created_by_reset)
    # Брони — у нового проживания номера; запрос прошлой картины без ответа закрыт.
    old_waiting = [rid for rid, status in before.bookings.items() if status in open_kinds]
    assert len(old_waiting) == 1
    assert after.bookings[old_waiting[0]] is RequestStatus.CANCELLED
    assert sum(status in open_kinds for status in after.bookings.values()) == 1
    assert await _stages(demo_tenant) == _THREE_STAGES


async def test_next_day_closes_yesterdays_leftovers(demo_tenant: uuid.UUID) -> None:
    await run_showcase(confirmed=True)
    yesterday = await _picture(demo_tenant)

    await run_showcase(confirmed=True, now=utc_now() + timedelta(days=1))

    with tenant_context(demo_tenant):
        open_requests = await requests_api.list_open_requests(limit=100)
        open_paths = await housekeeping_api.list_open_turnovers(limit=100)
        stays = await guests_api.list_active_stays()
    waiting = sum(1 for spec in DEMO_BOOKINGS if spec.answer is None)
    assert len(open_requests) == waiting + sum(
        1 for s in DEMO_REQUESTS if s.status in {RequestStatus.NEW, RequestStatus.IN_PROGRESS}
    )
    assert not {r.id for r in open_requests} & {*yesterday.requests, *yesterday.bookings}
    assert {p.room_number for p in open_paths} == set(yesterday.open_paths)
    yesterday_ids = {path_id for path_id, _ in yesterday.open_paths.values()}
    assert not {p.id for p in open_paths} & yesterday_ids
    assert {s.room_number for s in stays} == {spec.room for spec in DEMO_STAYS}


async def test_floors_and_steps_go_to_staff_who_accepted(demo_tenant: uuid.UUID) -> None:
    foreign = await create_staff_user(
        "HKSEVEN",
        tenant_id=demo_tenant,
        role=StaffRole.STAFF,
        display_name="Горничная 7",
        service_keys=[HOUSEKEEPING_SERVICE_KEY],
    )
    with tenant_context(demo_tenant):
        await housekeeping_api.replace_floor_assignments({foreign: frozenset({7})})
    lines = await run_showcase(confirmed=True)
    assert any("ещё не приняли" in line for line in lines)
    for token in _invite_tokens(lines).values():
        await accept_invite(token, password=PASSWORD)

    lines = await run_showcase(confirmed=True, reset=True)

    assert sum("вход уже есть" in line for line in lines) == len(DEMO_STAFF)
    assert not await list_pending_invites(demo_tenant)
    names = {m.user_id: m.display_name for m in await list_tenant_members(demo_tenant)}
    with tenant_context(demo_tenant):
        floors = await housekeeping_api.list_floor_assignments()
        open_paths = await housekeeping_api.list_open_turnovers(limit=100)
        ready = await housekeeping_api.list_turnovers_ready_since(_midnight(), limit=100)
        requests = await requests_api.list_open_requests(limit=100)
    assert {(names[row.user_id], row.floor) for row in floors} == {
        ("Горничная 7", 7),
        ("Горничная 15", 15),
        ("Горничная 16", 16),
    }
    cleaned = {p.room_number: p.cleaned_by_display_name for p in open_paths}
    assert cleaned["1504"] == "Горничная 15"
    reported = {p.room_number: p.reported_by_display_name for p in open_paths}
    assert reported["1601"] == "Ресепшен Демо"
    # Принятые до входа старшей номера — на имени менеджера: ready не пересоздаётся.
    assert {p.accepted_by_display_name for p in ready} == {"Менеджер Демо"}
    claimed = {r.room_number: r.claimed_by_display_name for r in requests}
    assert claimed["1512"] == "Горничная 15"


async def test_show_day_fills_sent_tab_and_stats_rows(demo_tenant: uuid.UUID) -> None:
    """Порядок рунбука (deploy.md, «Демо-картина к показу»): накануне — приглашения
    и вход, в день показа — свежая картина. Её заявки создают и двигают вошедшие."""
    for token in _invite_tokens(await run_showcase(confirmed=True)).values():
        await accept_invite(token, password=PASSWORD)
    show_day = utc_now() + timedelta(days=1)

    await run_showcase(confirmed=True, reset=True, now=show_day)

    names = {m.user_id: m.display_name for m in await list_tenant_members(demo_tenant)}
    with tenant_context(demo_tenant):
        sent = await requests_api.list_requests_sent_by_services(
            frozenset({HOUSEKEEPING_SERVICE_KEY}), limit=100
        )
        reception_sent = await requests_api.list_requests_sent_by_services(
            frozenset({RECEPTION_SERVICE_KEY}), limit=100
        )
        # Картина «дня показа» создана сейчас — её шаги в журнале за последнюю минуту.
        stats = await requests_api.staff_stats(
            since=utc_now() - timedelta(minutes=1),
            until=utc_now() + timedelta(seconds=1),
            category_ids=None,
        )
    # «Отправленные» уборки (spec 0063): поломки, найденные горничными на уборке.
    assert {(r.room_number, r.summary, r.status) for r in sent} == {
        ("1503", "Не горит свет в ванной", RequestStatus.IN_PROGRESS),
        ("1602", "Сломана ручка балконной двери", RequestStatus.DONE),
    }
    # Звонки гостей ресепшен отправил в службы — «Отправленные» ресепшена.
    by_reception = [s for s in DEMO_REQUESTS if s.sender == ("DEMOREC",)]
    assert {(r.room_number, r.summary) for r in reception_sent} >= {
        (s.room, s.summary) for s in by_reception
    }
    # «Статистика» (spec 0065): у каждого, кто нажимает шаги, есть «взято» и «выполнено».
    by_name = {names[row.user_id]: (row.claimed, row.done) for row in stats}
    assert by_name["Горничная 15"] == (2, 1)
    assert by_name["Горничная 16"] == (1, 1)
    assert by_name["Ресепшен Демо"] == (2, 1)


async def test_refuses_without_manager(
    canonical_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, STAGING_ENV)
    await seed_demo_data()
    with pytest.raises(DemoShowcaseError, match="нет активного менеджера"):
        await run_showcase(confirmed=True)


@pytest.mark.parametrize(
    ("override", "confirmed"),
    [
        ({"SENTRY_ENVIRONMENT": "dev"}, True),
        ({"SENTRY_ENVIRONMENT": "pilot"}, True),  # копия отеля (#389)
        ({}, False),
        # Копия отеля без строки SENTRY_ENVIRONMENT=pilot в .env — тоже staging (#389),
        # отель контура — свой.
        ({"TELEGRAM_TENANT_SLUG": "pilot-hotel"}, True),
        ({"PUBLIC_BASE_URL": "https://app.example.test"}, True),
    ],
)
async def test_refuses_outside_staging_and_touches_nothing(
    demo_tenant: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, str],
    confirmed: bool,
) -> None:
    _set_env(monkeypatch, override)

    with pytest.raises(DemoShowcaseError, match="Это не staging"):
        await run_showcase(confirmed=confirmed)

    with tenant_context(demo_tenant):
        assert not await guests_api.list_active_stays()
    assert not await list_pending_invites(demo_tenant)


def test_main_refuses_without_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    assert "--i-know-this-is-staging" in capsys.readouterr().err


def test_check_out_today_after_noon_stays_today() -> None:
    afternoon = datetime(2026, 11, 5, 15, 20, tzinfo=ZONE)
    assert check_out_moment(0, ZONE, afternoon) == datetime(2026, 11, 5, 17, 0, tzinfo=ZONE)
    late = datetime(2026, 11, 5, 22, 43, tzinfo=ZONE)
    assert check_out_moment(0, ZONE, late) == datetime(2026, 11, 5, 23, 59, tzinfo=ZONE)
    morning = datetime(2026, 11, 5, 9, 0, tzinfo=ZONE)
    assert check_out_moment(0, ZONE, morning) == datetime(2026, 11, 5, 12, 0, tzinfo=ZONE)
    assert check_out_moment(2, ZONE, morning) == datetime(2026, 11, 7, 12, 0, tzinfo=ZONE)


async def test_without_table_booking_the_picture_has_no_bookings(demo_tenant: uuid.UUID) -> None:
    async with platform_session_scope() as session:
        await mutate_tenant_config(
            session,
            demo_tenant,
            lambda config: config.model_copy(
                update={
                    "table_booking": config.table_booking.model_copy(update={"enabled": False})
                    if config.table_booking
                    else None
                }
            ),
        )

    report = await build_showcase(confirmed=True, reset=False, invite=False)

    assert report.bookings == 0
    assert any("Брони столика: у демо-отеля выключены" in line for line in report.lines)
    assert len(report.overdue_after) == 2  # только заявки картины: техслужба и уборка
    with tenant_context(demo_tenant):
        open_requests = await requests_api.list_open_requests(
            limit=100, room_number=DEMO_BOOKING_ROOM
        )
    assert not open_requests


def _option(*days: tuple[date, list[str]]) -> table_booking_api.RestaurantOption:
    return table_booking_api.RestaurantOption(
        key="terrace",
        names=LocalizedText(en="Terrace"),
        note=None,
        max_party=8,
        days=[table_booking_api.BookingDay(date=day, slots=slots) for day, slots in days],
    )


def test_slot_is_today_from_wanted_hour_else_tomorrow() -> None:
    today, tomorrow = date(2026, 11, 5), date(2026, 11, 6)
    evening = _option((today, ["19:00", "19:30", "20:00"]), (tomorrow, ["12:00", "19:30"]))
    assert pick_slot(evening, time(19, 30)) == (today, time(19, 30))
    # Сейчас позже желаемого часа — ближайший слот после него, тоже сегодня.
    late = _option((today, ["21:00", "21:30"]), (tomorrow, ["12:00", "19:30"]))
    assert pick_slot(late, time(19, 30)) == (today, time(21, 0))
    # Сегодня слотов не осталось — завтра в желаемый час.
    closed = _option((today, []), (tomorrow, ["12:00", "19:30", "20:00"]))
    assert pick_slot(closed, time(19, 30)) == (tomorrow, time(19, 30))
    # Желаемый час позже всех часов ресторана (конфиг правили руками) — первый слот.
    early = _option((today, ["12:00", "12:30"]))
    assert pick_slot(early, time(19, 30)) == (today, time(12, 0))
    assert pick_slot(_option((today, [])), time(19, 30)) is None
