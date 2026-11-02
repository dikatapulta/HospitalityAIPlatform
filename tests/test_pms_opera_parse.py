"""Разбор файлов отчётов Opera: что принимается и за что файл отвергается.

Чистые юниты без БД: `parse_report` — снимок «кто в доме» (spec 0058 §2.4),
`parse_availability` — свободные номера по ночам (spec 0059 §2.1). Маршруты,
коды ответов и хранение поверх разбора — `test_pms_opera_report.py`.
"""

from __future__ import annotations

from datetime import date

import pytest

from hospitality.integrations.pms.opera_report.parse import (
    ReportRejectedError,
    parse_availability,
    parse_report,
)

HEADER = "room,confirmation_no,arrival,departure,guests"
BOM = chr(0xFEFF)
ARABIC_THREE = chr(0x0663)  # isdigit() — да, но число гостей — только ASCII-цифры


def _parse(text: str) -> list[tuple[str, str, date, date, int | None]]:
    return [
        (row.room_number, row.booking_ref, row.arrival, row.departure, row.guests_count)
        for row in parse_report(text.encode(), max_guests=20)
    ]


def test_contract_file_becomes_rows() -> None:
    text = f"{HEADER}\n305,123456,2026-11-02,2026-11-04,2\n410,654321,2026-11-01,2026-11-06,\n"
    assert _parse(text) == [
        ("305", "123456", date(2026, 11, 2), date(2026, 11, 4), 2),
        ("410", "654321", date(2026, 11, 1), date(2026, 11, 6), None),
    ]


@pytest.mark.parametrize(
    "text",
    [
        # BOM, точка с запятой, даты через точку, заголовок с регистром и пробелами.
        BOM + "Room ; Confirmation_No ; Arrival ; Departure\n305;123456;02.11.2026;04.11.2026\n",
        # Табуляция, другой порядок колонок, пустые строки в конце, CRLF.
        "departure\tarrival\tconfirmation_no\troom\r\n2026-11-04\t2026-11-02\t123456\t305\r\n\r\n",
    ],
)
def test_contract_variants_are_accepted(text: str) -> None:
    assert _parse(text) == [("305", "123456", date(2026, 11, 2), date(2026, 11, 4), None)]


def test_header_only_is_an_empty_house() -> None:
    assert _parse(f"{HEADER}\n") == []


def test_same_day_departure_is_accepted() -> None:
    assert _parse(f"{HEADER}\n305,1,2026-11-02,2026-11-02,1\n")[0][3] == date(2026, 11, 2)


@pytest.mark.parametrize(
    ("body", "reason", "line"),
    [
        (b"\xff\xferoom", "encoding", None),
        (b"", "header", 1),
        (b"\nroom,confirmation_no,arrival,departure\n", "header", 1),
        (b"room confirmation_no arrival departure\n", "delimiter", 1),
        (b"room,confirmation_no;arrival,departure\n", "delimiter", 1),
        (b"room,confirmation_no,arrival,departure,name\n", "extra_column", 1),
        (b"room,confirmation_no,arrival\n", "missing_column", 1),
        (b"room,room,confirmation_no,arrival,departure\n", "header", 1),
        (f"{HEADER}\n305,1,2026-11-02\n".encode(), "row_shape", 2),
        (f"{HEADER}\n305,1,2026-11-02,2026-11-04,2,extra\n".encode(), "row_shape", 2),
        (f'{HEADER}\n"305,1,2026-11-02,2026-11-04,2\n'.encode(), "row_shape", 2),
        (f"{HEADER}\n305,,2026-11-02,2026-11-04,2\n".encode(), "empty_cell", 2),
        (f"{HEADER}\n ,1,2026-11-02,2026-11-04,2\n".encode(), "empty_cell", 2),
        (f"{HEADER}\n{'3' * 21},1,2026-11-02,2026-11-04,2\n".encode(), "room", 2),
        (f"{HEADER}\n305,{'9' * 65},2026-11-02,2026-11-04,2\n".encode(), "booking_ref", 2),
        (f"{HEADER}\n305,1,2026/11/02,2026-11-04,2\n".encode(), "date", 2),
        (f"{HEADER}\n305,1,30.02.2026,04.03.2026,2\n".encode(), "date", 2),
        (f"{HEADER}\n305,1,2026-11-04,2026-11-02,2\n".encode(), "dates_order", 2),
        (f"{HEADER}\n305,1,2026-11-02,2026-11-04,0\n".encode(), "guests", 2),
        (f"{HEADER}\n305,1,2026-11-02,2026-11-04,21\n".encode(), "guests", 2),
        (f"{HEADER}\n305,1,2026-11-02,2026-11-04,two\n".encode(), "guests", 2),
        (f"{HEADER}\n305,1,2026-11-02,2026-11-04,{ARABIC_THREE}\n".encode(), "guests", 2),
        (
            f"{HEADER}\n305,1,2026-11-02,2026-11-04,2\n410,1,2026-11-02,2026-11-05,1\n".encode(),
            "duplicate_booking",
            3,
        ),
    ],
)
def test_contract_violation_rejects_the_whole_file(
    body: bytes, reason: str, line: int | None
) -> None:
    with pytest.raises(ReportRejectedError) as caught:
        parse_report(body, max_guests=20)
    assert (caught.value.reason, caught.value.line) == (reason, line)


def test_rejection_names_the_class_not_the_content() -> None:
    """Причина — классом и номером строки: номер брони и лишняя колонка в текст не идут."""
    body = f"{HEADER}\n305,BOOKREFMARKERQZ,2026-11-04,2026-11-02,2\n".encode()
    with pytest.raises(ReportRejectedError) as caught:
        parse_report(body, max_guests=20)
    assert "BOOKREFMARKERQZ" not in str(caught.value)
    assert str(caught.value).startswith("line 2: departure is before arrival")

    with pytest.raises(ReportRejectedError) as caught:
        parse_report(b"room,confirmation_no,arrival,departure,GUESTNAMEMARKERQZ\n", max_guests=20)
    assert "GUESTNAMEMARKERQZ" not in str(caught.value)


def test_guest_ceiling_comes_from_the_caller() -> None:
    body = f"{HEADER}\n305,1,2026-11-02,2026-11-04,5\n".encode()
    assert parse_report(body, max_guests=5)[0].guests_count == 5
    with pytest.raises(ReportRejectedError, match="guests"):
        parse_report(body, max_guests=4)


# --- Свободные номера по ночам (spec 0059 §2.1, §5 п. 1) --------------------

AVAILABILITY = "date,free_rooms"


def _nights(text: str) -> list[tuple[date, int]]:
    return [(night.night, night.free_rooms) for night in parse_availability(text.encode())]


def test_availability_file_becomes_nights() -> None:
    text = f"{AVAILABILITY}\n2026-11-02,12\n2026-11-03,0\n2026-11-04,-3\n"
    assert _nights(text) == [
        (date(2026, 11, 2), 12),
        (date(2026, 11, 3), 0),
        (date(2026, 11, 4), -3),  # овербукинг: мест нет
    ]


def test_availability_variants_are_accepted() -> None:
    """BOM, `;`, даты через точку, колонки наоборот, строки не по порядку, пустая строка."""
    text = BOM + "Free_Rooms ; Date\n5;03.11.2026\n7;02.11.2026\n\n"
    assert _nights(text) == [(date(2026, 11, 2), 7), (date(2026, 11, 3), 5)]


def test_availability_takes_up_to_62_nights() -> None:
    lines = [f"{date.fromordinal(date(2026, 11, 2).toordinal() + day)},1" for day in range(62)]
    assert len(_nights("\n".join([AVAILABILITY, *lines]))) == 62
    with pytest.raises(ReportRejectedError) as caught:
        parse_availability("\n".join([AVAILABILITY, *lines, "2027-01-03,1"]).encode())
    assert (caught.value.reason, caught.value.line) == ("nights_count", None)


@pytest.mark.parametrize(
    ("text", "reason", "line"),
    [
        ("date,free_rooms,room\n", "columns", 1),
        ("date,rooms\n", "columns", 1),
        ("night,free_rooms\n", "columns", 1),
        ("date,date\n", "header", 1),
        ("date free_rooms\n", "delimiter", 1),
        (f"{AVAILABILITY}\n", "nights_count", None),
        (f"{AVAILABILITY}\n2026-11-02\n", "row_shape", 2),
        (f"{AVAILABILITY}\n2026-11-02,\n", "empty_cell", 2),
        (f"{AVAILABILITY}\n2026/11/02,4\n", "date", 2),
        (f"{AVAILABILITY}\n2026-11-02,4.5\n", "free_rooms", 2),
        (f"{AVAILABILITY}\n2026-11-02,10000\n", "free_rooms", 2),
        (f"{AVAILABILITY}\n2026-11-02,-1000\n", "free_rooms", 2),
        (f"{AVAILABILITY}\n2026-11-02,+4\n", "free_rooms", 2),
        (f"{AVAILABILITY}\n2026-11-02,{ARABIC_THREE}\n", "free_rooms", 2),
        (f"{AVAILABILITY}\n2026-11-02,4\n02.11.2026,5\n", "duplicate_night", 3),
        (f"{AVAILABILITY}\n2026-11-02,4\n2026-11-04,5\n", "night_gap", 3),
    ],
)
def test_availability_violation_rejects_the_whole_file(
    text: str, reason: str, line: int | None
) -> None:
    with pytest.raises(ReportRejectedError) as caught:
        parse_availability(text.encode())
    assert (caught.value.reason, caught.value.line) == (reason, line)


def test_availability_edges_of_free_rooms_are_accepted() -> None:
    text = f"{AVAILABILITY}\n2026-11-02,-999\n2026-11-03,9999\n"
    assert [count for _, count in _nights(text)] == [-999, 9999]
