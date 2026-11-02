"""Продление на доске выездов (spec 0062 §3, §8 PR 3, issue #668).

Правило: выезд до продления сегодня — да; просроченный — да; выезд завтра — нет;
открытый путь при выезде завтра — да; новая дата сегодня (поздний выезд) — нет;
продлён вчера — нет. Доска: блок «Продлили — выезда не будет» и метка на пути.
Сигнал: адресаты как у срочного до «Убрано» — команда этажа и старшая, не соседка;
без этажей — вся служба; ресепшен и менеджер — пусто, ресепшену список продлений
не читается; второе продление — новый ключ; выключенная доска — пусто. Пуш — без даты.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from hospitality.modules.guests import api as guests_api
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import push_texts, signal, turnover_extensions
from hospitality.staff_portal.floors import FloorRouting
from hospitality.staff_portal.tests.conftest import PortalHotel, store_hotel_config
from hospitality.staff_portal.tests.test_turnover_deadline import (
    ALMATY,
    TEN_AM,
    _audience,
    _freeze,
    _heard,
)
from hospitality.staff_portal.tests.test_turnovers import (
    BOARD,
    HOUSEKEEPING,
    RECEPTION,
    _categories,
    _login_as,
    _open_paths,
    _seed_path,
)

FLOOR_15 = FloorRouting(staffed_floors=frozenset({15, 16}), my_floors=frozenset({15}))
FLOOR_16 = FloorRouting(staffed_floors=frozenset({15, 16}), my_floors=frozenset({16}))


@pytest.fixture(autouse=True)
def _board_enabled() -> Iterator[None]:
    """Доска включена во всех тестах файла — канон фикстуры `test_turnovers`."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_TURNOVER_BOARD", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


def _at(day: int, hour: int = 12) -> datetime:
    """Час января 2030 по Алматы; TEN_AM — 10:00 15-го."""
    return datetime(2030, 1, day, hour, tzinfo=ALMATY)


def _clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Часы кабинета, уборки и модуля guests: продление пишет `extended_at` по ним."""
    _freeze(monkeypatch, moment)
    monkeypatch.setattr("hospitality.modules.guests.service.utc_now", lambda: moment)


async def _extended(
    portal_hotel: PortalHotel, room: str, was: datetime, until: datetime
) -> uuid.UUID:
    """Заселение со сроком `was` и продление «+N ночей» кабинета до `until`."""
    with tenant_context(portal_hotel.tenant_id):
        stay = await guests_api.check_in(guests_api.StayCheckIn(room_number=room, check_out_at=was))
        await guests_api.extend_stay(stay.stay.id, until)
    return stay.stay.id


async def _house(portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Шесть продлений правила §3; вернуть путь номера 1504 (выезд завтра, путь открыт)."""
    await store_hotel_config(portal_hotel.tenant_id, reminder_after_minutes=30)
    await _categories(portal_hotel)
    _clock(monkeypatch, TEN_AM - timedelta(days=1))
    await _extended(portal_hotel, "1506", _at(15), _at(17))  # продлён вчера
    _clock(monkeypatch, TEN_AM)
    await _extended(portal_hotel, "1501", _at(15), _at(17))  # выезд был сегодня
    await _extended(portal_hotel, "1502", _at(14), _at(17))  # просроченный
    await _extended(portal_hotel, "1503", _at(16), _at(18))  # выезд был завтра
    await _extended(portal_hotel, "1504", _at(16), _at(18))  # завтра, но путь открыт
    await _extended(portal_hotel, "1505", _at(15), _at(15, 16))  # поздний выезд сегодня
    return await _seed_path(portal_hotel, "1504")


async def test_rule_picks_due_today_overdue_and_open_path(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _house(portal_hotel, monkeypatch)
    open_paths = await _open_paths(portal_hotel)
    with tenant_context(portal_hotel.tenant_id):
        found = await turnover_extensions.housekeeping_extensions(open_paths, ALMATY, TEN_AM)
    assert [stay.room_number for stay in found] == ["1501", "1502", "1504"]


async def test_board_block_and_label_on_open_path(
    client: AsyncClient, portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _house(portal_hotel, monkeypatch)
    await _login_as(client, portal_hotel, StaffRole.STAFF, RECEPTION)

    board = (await client.get(BOARD)).text

    assert "Продлили — выезда не будет" in board
    for line in (
        "1501 — продлено до 17.01",
        "1502 — продлено до 17.01",
        "1504 — продлено до 18.01",
    ):
        assert line in board
    for room in ("1503", "1505", "1506"):
        assert f"{room} — продлено" not in board
    assert board.index("Продлили — выезда не будет") > board.index('id="turnover-reported"')
    assert (
        '<span class="badge badge-extended" data-extended>Продлено до 18.01 — гость остаётся'
        in (board)
    )
    # После полуночи отеля блок пуст.
    _clock(monkeypatch, _at(16, 0) + timedelta(minutes=1))
    assert "Продлили — выезда не будет" not in (await client.get(BOARD)).text


async def test_extension_calls_floor_team_and_seniors_only(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _house(portal_hotel, monkeypatch)
    extended = {("1501", "extended"), ("1502", "extended"), ("1504", "extended")}

    floor_15 = await _heard(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, FLOOR_15)
    assert floor_15 - {("1504", "inspect")} == extended
    assert await _heard(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, FLOOR_16) == set()
    assert extended <= await _heard(portal_hotel, StaffRole.STAFF, HOUSEKEEPING)
    assert extended <= await _heard(portal_hotel, StaffRole.SENIOR, HOUSEKEEPING, FLOOR_16)
    assert await _heard(portal_hotel, StaffRole.MANAGER, ()) == set()

    async def unread(since: datetime) -> list[guests_api.StayRead]:
        raise AssertionError("ресепшену список продлений не читается")

    monkeypatch.setattr(guests_api, "list_recent_extensions", unread)
    assert await _heard(portal_hotel, StaffRole.STAFF, RECEPTION) == set()


async def test_second_extension_is_a_new_key_and_board_off_is_silent(
    portal_hotel: PortalHotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _house(portal_hotel, monkeypatch)
    senior = _audience(portal_hotel, StaffRole.SENIOR, HOUSEKEEPING)

    async def key_1504() -> list[str]:
        with tenant_context(portal_hotel.tenant_id):
            view = await signal.build_signal(senior)
        return [
            call.journal_key()
            for call in view.turnovers
            if call.kind == "extended" and call.room_number == "1504"
        ]

    first = await key_1504()
    with tenant_context(portal_hotel.tenant_id):
        stay = await guests_api.find_active_stay("1504")
        assert stay is not None
        await guests_api.extend_stay(stay.id, _at(20))
    second = await key_1504()
    assert len(first) == len(second) == 1 and first != second

    monkeypatch.setenv("ENABLE_TURNOVER_BOARD", "false")
    get_settings.cache_clear()
    assert await _heard(portal_hotel, StaffRole.SENIOR, HOUSEKEEPING) == set()


def test_push_has_no_date() -> None:
    call = signal.TurnoverCallView(
        id="s1", room_number="1504", kind="extended", extended_until=_at(17).astimezone(UTC)
    )
    message = push_texts.turnover_call(call, "/board")
    assert (message.title, message.body) == (
        "1504 — продлили",
        "Гость остаётся — выезда сегодня не будет",
    )
    later = call.model_copy(update={"extended_until": _at(18).astimezone(UTC)})
    assert call.journal_key() != later.journal_key()
