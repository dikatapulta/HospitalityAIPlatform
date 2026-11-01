"""Разбор файла отчёта Opera (spec 0058 §2.4): что принимается и за что файл отвергается.

Чистые юниты `parse_report`, без БД. Маршрут, коды ответов и снимок поверх
разбора — `test_pms_opera_report.py`.
"""

from __future__ import annotations

from datetime import date

import pytest

from hospitality.integrations.pms.opera_report.parse import ReportRejectedError, parse_report

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
