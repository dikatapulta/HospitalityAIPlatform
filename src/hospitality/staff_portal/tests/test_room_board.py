"""Доска «Номера»: статус клетки и полосы (spec 0061 §3, §5, §9) — без базы.

`build_room_board` — чистая функция: правила статуса по порядку, порядок
номеров и рядов, «Без этажа» и «Нет в списке номеров». Полоса и «свободен»
смотрят на один признак — `knows_vacancy`. Страница — `test_room_board_page.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from hospitality.modules.guests import api as guests_api
from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform.config import HotelProfile, TenantConfig
from hospitality.platform.hotel_rooms import HotelRoom
from hospitality.staff_portal import room_board

ZONE = ZoneInfo("Asia/Almaty")
# 05.11 10:00 по Алматы: «сегодня» доски — 05.11.
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=ZONE)
TurnoverStatus = housekeeping_api.TurnoverStatus


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


def _stay(
    room: str,
    *,
    check_in: datetime | None = None,
    check_out: datetime | None = None,
    guests: int = 2,
) -> guests_api.StayRead:
    return guests_api.StayRead(
        id=uuid.uuid4(),
        guest_id=uuid.uuid4(),
        room_number=room,
        status=guests_api.StayStatus.CHECKED_IN,
        guests_count=guests,
        check_in_at=check_in or _at(3, 14),
        check_out_at=check_out or _at(7, 12),
    )


def _turnover(
    room: str,
    status: TurnoverStatus = TurnoverStatus.REPORTED,
    *,
    reported_at: datetime | None = None,
    ready_at: datetime | None = None,
    note: str | None = None,
) -> housekeeping_api.RoomTurnoverRead:
    return housekeeping_api.RoomTurnoverRead(
        id=uuid.uuid4(),
        room_number=room,
        status=status,
        inspection_note=note,
        reported_at=reported_at or _at(5, 9, 40),
        reported_by_display_name="Алия",
        inspected_at=None,
        inspected_by_display_name=None,
        cleaning_started_at=None,
        cleaning_started_by_display_name=None,
        cleaned_at=None,
        cleaned_by_display_name=None,
        ready_at=ready_at,
        accepted_by_display_name="Дина" if ready_at else None,
        cancelled_at=None,
        cancelled_by_display_name=None,
    )


def _rooms(*numbers: str) -> tuple[HotelRoom, ...]:
    return tuple(HotelRoom(number=number) for number in numbers)


def _board(
    rooms: tuple[HotelRoom, ...],
    *,
    stays: tuple[guests_api.StayRead, ...] = (),
    open_turnovers: tuple[housekeeping_api.RoomTurnoverRead, ...] = (),
    ready: tuple[housekeeping_api.RoomTurnoverRead, ...] = (),
    vacancy: bool = True,
) -> room_board.RoomBoard:
    return room_board.build_room_board(
        rooms, stays, open_turnovers, ready, vacancy_known=vacancy, local_now=NOW
    )


def _cells(board: room_board.RoomBoard) -> dict[str, room_board.Cell]:
    return {cell.number: cell for row in board.rows for cell in row.cells}


def test_each_status_in_rule_order() -> None:
    board = _board(
        _rooms("1401", "1402", "1403", "1404", "1405", "1406", "1407"),
        stays=(
            _stay("1402", guests=3),
            _stay("1403", check_out=_at(5, 12), guests=1),
            _stay("1404", check_out=_at(1, 12)),
        ),
        open_turnovers=(_turnover("1401", TurnoverStatus.CLEANING, note="Разбит стакан"),),
        ready=(_turnover("1405", TurnoverStatus.READY, ready_at=_at(5, 9, 15)),),
        vacancy=True,
    )
    cells = _cells(board)

    assert {number: cell.status for number, cell in cells.items()} == {
        "1401": "cleaning",
        "1402": "occupied",
        "1403": "due_out",
        "1404": "due_out",
        "1405": "ready",
        "1406": "vacant",
        "1407": "vacant",
    }
    assert cells["1401"].line == "выезд 09:40 · Алия"
    assert cells["1401"].note == "Разбит стакан"
    assert cells["1401"].turnover_id
    assert cells["1402"].line == "Гость до 07.11"
    assert cells["1403"].line == "Выезд сегодня в 12:00"
    assert cells["1404"].line == "Выезд был 01.11 в 12:00"
    assert cells["1405"].line == "готов 09:15 · Дина"
    assert cells["1406"].line == "Гостя нет"
    # Число гостей — только у живущих.
    assert [cells[n].guests for n in ("1401", "1402", "1403", "1405", "1406")] == [0, 3, 1, 0, 0]
    assert board.counts == {"cleaning": 1, "occupied": 1, "due_out": 2, "ready": 1, "vacant": 2}
    assert board.total == 7


def test_without_fresh_opera_empty_room_is_unknown_not_vacant() -> None:
    cell = _cells(_board(_rooms("1401"), vacancy=False))["1401"]

    assert cell.status == "unknown"
    assert cell.line == "В кабинете гостя нет — проверьте в Opera"


def test_turnover_left_from_previous_stay_yields_to_new_guest() -> None:
    """Путь открыт в 09:40, а гость заселён в 10:00 — номер «занят» (§3 п. 1)."""
    late = _stay("1401", check_in=_at(5, 10))
    early = _stay("1402", check_in=_at(4, 15))
    board = _board(
        _rooms("1401", "1402"),
        stays=(late, early),
        open_turnovers=(_turnover("1401"), _turnover("1402")),
    )
    cells = _cells(board)

    assert cells["1401"].status == "occupied"
    assert cells["1401"].turnover_id == ""
    assert cells["1402"].status == "reported"


def test_stay_beats_ready_and_latest_ready_wins() -> None:
    board = _board(
        _rooms("1401", "1402"),
        stays=(_stay("1401", check_in=_at(5, 9, 30)),),
        ready=(
            _turnover("1401", TurnoverStatus.READY, ready_at=_at(5, 9)),
            _turnover("1402", TurnoverStatus.READY, ready_at=_at(5, 9, 50)),
            _turnover("1402", TurnoverStatus.READY, ready_at=_at(5, 8)),
        ),
    )
    cells = _cells(board)

    assert cells["1401"].status == "occupied"
    assert cells["1402"].line == "готов 09:50 · Дина"


def test_rows_keep_list_order_then_no_floor_then_unlisted() -> None:
    rooms = (
        HotelRoom(number="1402", room_type="PRMT"),
        HotelRoom(number="1401", room_type="FAMR"),
        HotelRoom(number="Люкс"),
        HotelRoom(number="701"),
    )
    board = _board(
        rooms,
        stays=(_stay(" 1401 "), _stay("1410"), _stay("01401")),
        open_turnovers=(_turnover("905"),),
    )

    # Сторон в списке нет — у каждого ряда одна сторона без подписи.
    assert [(row.key, row.title, [side.place for side in row.sides]) for row in board.rows] == [
        ("7", "7", ["7 этаж"]),
        ("14", "14", ["14 этаж"]),
        (room_board.NO_FLOOR_KEY, "Без этажа", ["Без этажа"]),
        (room_board.UNLISTED_KEY, "Нет в списке номеров", ["Нет в списке номеров"]),
    ]
    assert {side.label for row in board.rows for side in row.sides} == {""}
    assert [row.is_floor for row in board.rows] == [True, True, False, False]
    floor_14, unlisted = board.rows[1], board.rows[3]
    assert [(cell.number, cell.room_type) for cell in floor_14.cells] == [
        ("1402", "PRMT"),
        ("1401", "FAMR"),
    ]
    # Крайние пробелы обрезаются, ведущий ноль — нет: «01401» не «1401».
    assert floor_14.cells[1].status == "occupied"
    assert [cell.number for cell in unlisted.cells] == ["905", "1410", "01401"]
    assert [cell.status for cell in unlisted.cells] == ["reported", "occupied", "occupied"]


def test_floor_splits_into_corridor_sides_in_list_order() -> None:
    """Стороны коридора (#674): ряд на сторону в порядке первого номера стороны,
    внутри — порядок списка; номер без стороны — свой ряд без подписи. «Нет в
    списке номеров» сторон не знает."""
    hills, town = "Вид на горы", "Вид на город"
    rooms = (
        HotelRoom(number="1403", room_type="PRMT", side=hills),
        HotelRoom(number="1424", room_type="PRMT", side=town),
        HotelRoom(number="1401", room_type="FAMR", side=hills),
        HotelRoom(number="1450"),
        HotelRoom(number="1422", side=town),
        HotelRoom(number="Люкс", side=hills),
    )
    board = _board(rooms, stays=(_stay("1403"), _stay("905")))

    floor_14, no_floor, unlisted = board.rows
    assert [
        (side.label, side.place, [cell.number for cell in side.cells]) for side in floor_14.sides
    ] == [
        (hills, "14 этаж · Вид на горы", ["1403", "1401"]),
        (town, "14 этаж · Вид на город", ["1424", "1422"]),
        ("", "14 этаж", ["1450"]),
    ]
    assert [cell.number for cell in floor_14.cells] == ["1403", "1401", "1424", "1422", "1450"]
    assert floor_14.sides[0].cells[0].status == "occupied"
    assert [(side.label, side.place) for side in no_floor.sides] == [
        (hills, "Без этажа · Вид на горы")
    ]
    assert [(side.label, side.place) for side in unlisted.sides] == [("", "Нет в списке номеров")]
    assert board.total == 7


def test_empty_list_shows_only_known_rooms_by_floor() -> None:
    board = _board(
        (),
        stays=(_stay("1401"), _stay("Люкс")),
        open_turnovers=(_turnover("705"),),
        vacancy=False,
    )

    assert [(row.key, [cell.number for cell in row.cells]) for row in board.rows] == [
        ("7", ["705"]),
        ("14", ["1401"]),
        (room_board.NO_FLOOR_KEY, ["Люкс"]),
    ]
    assert board.counts.get("unknown") is None


def _config(source: str) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile": HotelProfile(city="Almaty", country_code="KZ"),
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "pms_source": source,
        }
    )


def _status(
    *, taken: datetime | None, applied: datetime | None, stale: bool, applied_stale: bool
) -> guests_api.PmsStatus:
    return guests_api.PmsStatus(
        interval_minutes=5,
        taken_at=taken,
        stale=stale,
        held_closures=0,
        discrepancies=[],
        last_applied_at=applied,
        applied_stale=applied_stale,
    )


@pytest.mark.parametrize(
    ("taken", "applied", "stale", "applied_stale", "text"),
    [
        (None, None, False, False, "Данные из Opera ещё не приходили"),
        (
            _at(5, 9, 55),
            None,
            False,
            False,
            "Данные из Opera приходят, но кабинет их пока не обработал",
        ),
        (_at(5, 9, 55), _at(5, 9, 0), False, True, "Данные из Opera не обновлялись с 09:00"),
        # Применён свежо, а сам снимок давний (П2-1 ревью PR #671) — время снимка.
        (_at(4, 14, 5), _at(5, 9, 55), True, False, "Данные из Opera не обновлялись с 04.11 14:05"),
        (_at(5, 9, 55), _at(5, 9, 56), False, False, None),
    ],
)
def test_opera_band_and_vacancy_look_at_one_sign(
    taken: datetime | None,
    applied: datetime | None,
    stale: bool,
    applied_stale: bool,
    text: str | None,
) -> None:
    status = _status(taken=taken, applied=applied, stale=stale, applied_stale=applied_stale)
    config = _config("opera_report")

    band = room_board.source_text(config, status, NOW)

    if text is None:
        assert band is None
        assert room_board.knows_vacancy(config, status)
    else:
        assert band == text + " — свободные номера пока серые."
        assert not room_board.knows_vacancy(config, status)


def test_manual_hotel_never_knows_vacancy() -> None:
    fresh = _status(taken=_at(5, 9, 55), applied=_at(5, 9, 56), stale=False, applied_stale=False)

    assert not room_board.knows_vacancy(_config("manual"), fresh)
    assert not room_board.knows_vacancy(None, None)
    assert room_board.source_text(_config("manual"), None, NOW) == room_board.MANUAL_SOURCE_TEXT
    assert room_board.source_text(None, None, NOW) == room_board.MANUAL_SOURCE_TEXT
