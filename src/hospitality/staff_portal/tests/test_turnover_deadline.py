"""«Срочно к часу» на доске выездов (spec 0062 §2, §8 PR 2, issue #668).

Кабинет: право (ERR-HOUSEKEEPING-003 горничной без старшинства), время «11:00» —
сегодня по поясу отеля, прошедшее — 007, выключатель — 404. Доска: группа
«Срочно» первой по сроку, плитки без двойного счёта, «время вышло», дата у
вчерашней метки. Сигнал: до «Убрано» — команда этажа и старшая, не соседка; без
этажей — вся служба; убранный — только старшая; менеджер — пусто; новое время —
новый ключ, снятая — выпадает. Пуш — те же слова, что `signal.js`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import push_texts, signal, turnover_api, turnover_deadline, turnovers
from hospitality.staff_portal.floors import FloorRouting
from hospitality.staff_portal.tests.conftest import PortalHotel, store_hotel_config
from hospitality.staff_portal.tests.test_turnovers import (
    BOARD,
    HOUSEKEEPING,
    RECEPTION,
    _categories,
    _groups,
    _login_as,
    _seed_path,
    _step,
    _steps,
)

Status = housekeeping_api.TurnoverStatus
ALMATY = ZoneInfo("Asia/Almaty")
# 10:00 по Алматы, в будущем относительно настоящих часов: домен сверяет срок со
# своими часами, их тоже подменяем — день теста не зависит от часа прогона.
TEN_AM = datetime(2030, 1, 15, 5, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _board_enabled() -> Iterator[None]:
    """Доска включена во всех тестах файла — канон фикстуры `test_turnovers`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    for target in (turnover_api, turnovers, signal):
        monkeypatch.setattr(target, "utc_now", lambda: moment)
    monkeypatch.setattr("hospitality.modules.housekeeping.service.utc_now", lambda: moment)


async def _mark(
    client: AsyncClient, turnover_id: uuid.UUID, clock: str | None
) -> dict[str, object]:
    response = await _step(client, turnover_id, "deadline", {"needed_by": clock})  # type: ignore[dict-item]
    body: dict[str, object] = response.json()
    body["status_code"] = response.status_code
    return body


async def _walk(portal_hotel: PortalHotel, room: str, *steps: Status) -> uuid.UUID:
    turnover_id = await _seed_path(portal_hotel, room)
    with tenant_context(portal_hotel.tenant_id):
        for status in steps:
            await housekeeping_api.change_turnover_status(turnover_id, status)
    return turnover_id


async def _set(portal_hotel: PortalHotel, turnover_id: uuid.UUID, at: datetime | None) -> None:
    with tenant_context(portal_hotel.tenant_id):
        await housekeeping_api.set_turnover_deadline(turnover_id, at)


# ---------------------------------------------------------------------------
# Действие: право, время по отелю, выключатель
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "service_keys", "allowed"),
    [
        (StaffRole.STAFF, RECEPTION, True),
        (StaffRole.SENIOR, HOUSEKEEPING, True),
        (StaffRole.MANAGER, (), True),
        (StaffRole.STAFF, HOUSEKEEPING, False),
        (StaffRole.SENIOR, ("maintenance",), False),
    ],
)
async def test_deadline_right_by_level_and_service(
    client: AsyncClient,
    portal_hotel: PortalHotel,
    monkeypatch: pytest.MonkeyPatch,
    role: StaffRole,
    service_keys: tuple[str, ...],
    allowed: bool,
) -> None:
    _freeze(monkeypatch, TEN_AM)
    await store_hotel_config(portal_hotel.tenant_id)
    await _categories(portal_hotel)
    turnover_id = await _seed_path(portal_hotel)
    await _login_as(client, portal_hotel, role, service_keys)

    marked = await _mark(client, turnover_id, "11:00")

    if allowed:
        assert marked == {"room_number": "704", "needed_by": "11:00", "status_code": 200}
        board = (await client.get(BOARD)).text
        assert 'data-action="clear-deadline"' in board
        assert "Изменить время" in board
    else:
        assert marked["status_code"] == 403
        assert marked["error"]["code"] == "ERR-HOUSEKEEPING-003"  # type: ignore[index]


async def test_clock_is_today_in_hotel_zone_and_passed_is_refused(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, TEN_AM)
    await store_hotel_config(portal_hotel.tenant_id)
    await _categories(portal_hotel)
    turnover_id = await _seed_path(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    assert (await _mark(client, turnover_id, "11:30"))["status_code"] == 200
    with tenant_context(portal_hotel.tenant_id):
        stored = (await housekeeping_api.get_turnover(turnover_id)).needed_by_at
    assert stored == datetime(2030, 1, 15, 11, 30, tzinfo=ALMATY)
    for passed in ("09:00", "10:00"):
        refused = await _mark(client, turnover_id, passed)
        assert refused["status_code"] == 422
        assert refused["error"]["code"] == "ERR-HOUSEKEEPING-007"  # type: ignore[index]
    assert (await _mark(client, turnover_id, "25:00"))["status_code"] == 422
    cleared = await _mark(client, turnover_id, None)
    assert cleared == {"room_number": "704", "needed_by": None, "status_code": 200}


async def test_board_off_hides_the_action(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _categories(portal_hotel)
    turnover_id = await _seed_path(portal_hotel)
    await _login_as(client, portal_hotel, StaffRole.MANAGER, ())
    monkeypatch.setenv("ENABLE_TURNOVER_BOARD", "false")
    get_settings.cache_clear()

    assert (await _mark(client, turnover_id, "11:00"))["status_code"] == 404


# ---------------------------------------------------------------------------
# Доска
# ---------------------------------------------------------------------------


async def test_urgent_group_comes_first_by_deadline_without_double_count(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, TEN_AM)
    await store_hotel_config(portal_hotel.tenant_id)
    await _categories(portal_hotel)
    later = await _seed_path(portal_hotel, "1504")
    sooner = await _walk(portal_hotel, "1210", Status.INSPECTED, Status.CLEANING)
    await _seed_path(portal_hotel, "704")
    await _set(portal_hotel, later, TEN_AM + timedelta(hours=3))
    await _set(portal_hotel, sooner, TEN_AM + timedelta(hours=1))
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    board = (await client.get(BOARD)).text

    assert list(_groups(board)) == ["urgent", "reported"]
    assert _groups(board) == {"urgent": 2, "reported": 1}
    assert _steps(board) == {
        "urgent": 2,
        "reported": 1,
        "inspected": 0,
        "cleaning": 0,
        "cleaned": 0,
        "ready": 0,
    }
    assert board.index('data-room="1210"') < board.index('data-room="1504"')
    assert "Срочно к 11:00" in board and "Срочно к 13:00" in board
    # Шаг срочного — точкой и подписью группы.
    assert '<span class="step-dot step-cleaning" aria-hidden="true"></span>Убираются' in board

    _freeze(monkeypatch, TEN_AM + timedelta(hours=1, minutes=5))
    assert "Срочно к 11:00 · время вышло" in (await client.get(BOARD)).text
    _freeze(monkeypatch, TEN_AM + timedelta(days=1))
    assert "Срочно к 11:00, 15.01" in (await client.get(BOARD)).text


def test_labels_and_default_clock() -> None:
    now = datetime(2030, 1, 15, 10, 20, tzinfo=ALMATY)
    mark = datetime(2030, 1, 15, 11, 0, tzinfo=ALMATY)
    assert turnover_deadline.board_label(mark, ALMATY, now) == "Срочно к 11:00"
    assert turnover_deadline.default_clock(ALMATY, now) == "11:00"
    assert turnover_deadline.default_clock(ALMATY, now.replace(hour=23)) == ""
    assert turnover_deadline.today_at("11:00:00", ALMATY, now) == mark
    with pytest.raises(ValueError):
        turnover_deadline.today_at("11", ALMATY, now)


# ---------------------------------------------------------------------------
# Сигнал и пуш
# ---------------------------------------------------------------------------


def _audience(portal_hotel: PortalHotel, role: StaffRole, keys: tuple[str, ...]) -> StaffAudience:
    return StaffAudience(
        tenant_id=portal_hotel.tenant_id, role_key=role, service_keys=frozenset(keys)
    )


async def _heard(
    portal_hotel: PortalHotel,
    role: StaffRole,
    keys: tuple[str, ...],
    routing: FloorRouting | None = None,
) -> set[tuple[str, str]]:
    with tenant_context(portal_hotel.tenant_id):
        view = await signal.build_signal(
            _audience(portal_hotel, role, keys), routing or FloorRouting()
        )
    return {(call.room_number, call.kind) for call in view.turnovers}


async def test_urgent_calls_floor_team_and_senior_then_only_senior_when_cleaned(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """704 и 705 срочные: 704 ждёт проверки, 705 убран. Горничная 7-го этажа и
    старшая слышат 704 вместо «Проверьте», соседка с 8-го — нет; 705 — только
    старшая, вместо «Примите»; ресепшен и менеджер — ничего."""
    _freeze(monkeypatch, TEN_AM)
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    await _categories(portal_hotel)
    reported = await _seed_path(portal_hotel, "704")
    cleaned = await _walk(portal_hotel, "705", Status.INSPECTED, Status.CLEANING, Status.CLEANED)
    for turnover_id in (reported, cleaned):
        await _set(portal_hotel, turnover_id, TEN_AM + timedelta(hours=1))
    floor_7 = FloorRouting(staffed_floors=frozenset({7, 8}), my_floors=frozenset({7}))
    floor_8 = FloorRouting(staffed_floors=frozenset({7, 8}), my_floors=frozenset({8}))

    assert await _heard(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, floor_7) == {("704", "urgent")}
    assert await _heard(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, floor_8) == set()
    # Без этажей — вся служба.
    assert await _heard(portal_hotel, StaffRole.STAFF, HOUSEKEEPING) == {("704", "urgent")}
    assert await _heard(portal_hotel, StaffRole.SENIOR, HOUSEKEEPING, floor_8) == {
        ("704", "urgent"),
        ("705", "urgent"),
    }
    assert await _heard(portal_hotel, StaffRole.STAFF, RECEPTION) == set()
    assert await _heard(portal_hotel, StaffRole.MANAGER, ()) == set()


async def test_new_time_is_a_new_key_and_cleared_mark_drops_out(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, TEN_AM)
    await store_hotel_config(portal_hotel.tenant_id)
    await _categories(portal_hotel)
    turnover_id = await _seed_path(portal_hotel, "1504")
    senior = _audience(portal_hotel, StaffRole.SENIOR, HOUSEKEEPING)

    async def keys() -> list[str]:
        with tenant_context(portal_hotel.tenant_id):
            view = await signal.build_signal(senior)
        return [call.journal_key() for call in view.turnovers]

    assert await keys() == [f"{turnover_id}:inspect"]  # прежний ключ — без срока
    await _set(portal_hotel, turnover_id, TEN_AM + timedelta(hours=1))
    first = await keys()
    await _set(portal_hotel, turnover_id, TEN_AM + timedelta(hours=2))
    second = await keys()
    await _set(portal_hotel, turnover_id, None)

    assert first[0].startswith(f"{turnover_id}:urgent:") and len(first) == 1
    assert second != first
    assert await keys() == [f"{turnover_id}:inspect"]


def test_push_words_match_the_spec() -> None:
    call = signal.TurnoverCallView(
        id="t1",
        room_number="1504",
        kind="urgent",
        needed_by_at=TEN_AM,
        needed_by="11:00",
    )
    message = push_texts.turnover_call(call, "/board")
    assert (message.title, message.body) == ("1504 — срочно к 11:00", "Возьмите этот номер первым")
    cleaned = push_texts.turnover_call(call.model_copy(update={"cleaned": True}), "/board")
    assert cleaned.body == "Номер убран — примите его первым"
    assert call.journal_key() != call.model_copy(update={"cleaned": True}).journal_key()
