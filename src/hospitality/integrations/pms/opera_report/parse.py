"""Разбор файлов отчётов Opera: снимок «кто в доме» и свободные номера по ночам.

Общее у обоих файлов: UTF-8 (BOM допустим), заголовок первой строкой,
разделитель `,` `;` или табуляция, колонки в любом порядке, даты —
`YYYY-MM-DD` или `DD.MM.YYYY`. Любое нарушение — **отказ файлу целиком**
(`ReportRejectedError`).

- Снимок (`parse_report`, spec 0058 §2.4, ADR-004): колонки — ровно `room`,
  `confirmation_no`, `arrival`, `departure` и необязательная `guests`. Лишняя
  колонка не вычищается, а отвергает файл — решение основателя 06.08 «без имён»
  держится кодом, а не договорённостью (ADR-004).
- Свободные номера (`parse_availability`, spec 0059 §2.1): колонки — ровно
  `date` и `free_rooms`; ночи — подряд, без повторов и пропусков, от 1 до 62.

Причина отказа — класс и номер строки, без содержимого файла: ни ответ, ни
лог не несут номеров броней, комнат и того, что IT по ошибке выгрузил лишним.
"""

from __future__ import annotations

import csv
import re
from datetime import date, timedelta
from itertools import pairwise
from typing import Final, Literal

from hospitality.shared.ports.pms import (
    MAX_BOOKING_REF_LENGTH,
    MAX_ROOM_NUMBER_LENGTH,
    PmsNightAvailability,
    PmsStayRow,
)

RejectReason = Literal[
    "encoding",
    "header",
    "delimiter",
    "extra_column",
    "missing_column",
    "row_shape",
    "empty_cell",
    "room",
    "booking_ref",
    "date",
    "dates_order",
    "guests",
    "duplicate_booking",
    # Файл свободных номеров (spec 0059 §2.1).
    "columns",
    "free_rooms",
    "duplicate_night",
    "night_gap",
    "nights_count",
]

REQUIRED_COLUMNS: Final = ("room", "confirmation_no", "arrival", "departure")
OPTIONAL_COLUMN: Final = "guests"
AVAILABILITY_COLUMNS: Final = ("date", "free_rooms")
MAX_AVAILABILITY_NIGHTS: Final = 62
# У Opera с овербукингом свободных бывает меньше нуля; ≤ 0 — мест нет (spec 0059 §2.1).
MIN_FREE_ROOMS: Final = -999
MAX_FREE_ROOMS: Final = 9999
_DELIMITERS: Final = (",", ";", "\t")
_WHOLE_NUMBER: Final = re.compile(r"-?[0-9]{1,4}")
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_DOTTED_DATE: Final = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# Текст для IT отеля — в ответе маршрута и в журнале задачи планировщика.
_EXPLANATIONS: Final[dict[RejectReason, str]] = {
    "encoding": "the file is not UTF-8 text",
    "header": "the first line must be the column header, each column once",
    "delimiter": "the header must use exactly one delimiter: comma, semicolon or tab",
    "extra_column": "unexpected column: only room, confirmation_no, arrival, departure "
    "and guests are allowed",
    "missing_column": "a required column is missing: room, confirmation_no, arrival, departure",
    "row_shape": "the row has a different number of cells than the header",
    "empty_cell": "a required cell is empty",
    "room": f"room is longer than {MAX_ROOM_NUMBER_LENGTH} characters",
    "booking_ref": f"confirmation_no is longer than {MAX_BOOKING_REF_LENGTH} characters",
    "date": "a date is not YYYY-MM-DD or DD.MM.YYYY",
    "dates_order": "departure is before arrival",
    "guests": "guests is not a whole number in the allowed range",
    "duplicate_booking": "a confirmation_no repeats",
    "columns": "the columns must be exactly date and free_rooms",
    "free_rooms": f"free_rooms is not a whole number from {MIN_FREE_ROOMS} to {MAX_FREE_ROOMS}",
    "duplicate_night": "a date repeats",
    "night_gap": "a night is missing: the dates must follow one another day by day",
    "nights_count": f"the file must have from 1 to {MAX_AVAILABILITY_NIGHTS} nights",
}


class ReportRejectedError(ValueError):
    """Файл не по контракту: `reason` — класс причины, `line` — номер строки файла."""

    def __init__(self, reason: RejectReason, line: int | None = None) -> None:
        self.reason: RejectReason = reason
        self.line = line
        where = f"line {line}: " if line is not None else ""
        super().__init__(f"{where}{_EXPLANATIONS[reason]} (reason: {reason})")


def parse_report(body: bytes, *, max_guests: int) -> tuple[PmsStayRow, ...]:
    """Строки снимка из файла; файл не по контракту — `ReportRejectedError`.

    `max_guests` — потолок гостей одного Stay: его знает домен
    (`guests_api.MAX_GUESTS_PER_STAY`), адаптеру его передаёт composition root.
    Файл из одного заголовка — пустой дом, это снимок, а не отказ.
    """
    lines = _lines(body)
    reader = csv.reader(lines, delimiter=_delimiter(lines[0]), strict=True)
    try:
        columns = _columns(next(reader))
        rows: list[PmsStayRow] = []
        seen: set[str] = set()
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue  # пустая строка в конце файла — не бронь
            row = _row(cells, columns, reader.line_num, max_guests)
            if row.booking_ref in seen:
                raise ReportRejectedError("duplicate_booking", reader.line_num)
            seen.add(row.booking_ref)
            rows.append(row)
    except csv.Error:
        raise ReportRejectedError("row_shape", reader.line_num) from None
    return tuple(rows)


def parse_availability(body: bytes) -> tuple[PmsNightAvailability, ...]:
    """Ночи из файла свободных номеров; файл не по контракту — `ReportRejectedError`.

    Строки файла могут идти в любом порядке, но ночи вместе — подряд, без повторов
    и пропусков: «нет строки» не значит «мест нет», и порт принимает ночи только так.
    """
    lines = _lines(body)
    reader = csv.reader(lines, delimiter=_delimiter(lines[0]), strict=True)
    nights: dict[date, tuple[int, int]] = {}  # ночь → (свободно номеров, строка файла)
    try:
        columns = _availability_columns(next(reader))
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue  # пустая строка в конце файла — не ночь
            night, free_rooms = _night(cells, columns, reader.line_num)
            if night in nights:
                raise ReportRejectedError("duplicate_night", reader.line_num)
            nights[night] = (free_rooms, reader.line_num)
    except csv.Error:
        raise ReportRejectedError("row_shape", reader.line_num) from None
    if not 1 <= len(nights) <= MAX_AVAILABILITY_NIGHTS:
        raise ReportRejectedError("nights_count")
    ordered = sorted(nights)
    for previous, current in pairwise(ordered):
        if current - previous != timedelta(days=1):
            raise ReportRejectedError("night_gap", nights[current][1])
    return tuple(
        PmsNightAvailability(night=night, free_rooms=nights[night][0]) for night in ordered
    )


def _lines(body: bytes) -> list[str]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ReportRejectedError("encoding") from None
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ReportRejectedError("header", 1)
    return lines


def _delimiter(header: str) -> str:
    present = [candidate for candidate in _DELIMITERS if candidate in header]
    if len(present) != 1:
        raise ReportRejectedError("delimiter", 1)
    return present[0]


def _columns(header: list[str]) -> dict[str, int]:
    names = [name.strip().lower() for name in header]
    if len(set(names)) != len(names):
        raise ReportRejectedError("header", 1)
    if any(name not in (*REQUIRED_COLUMNS, OPTIONAL_COLUMN) for name in names):
        raise ReportRejectedError("extra_column", 1)
    if any(name not in names for name in REQUIRED_COLUMNS):
        raise ReportRejectedError("missing_column", 1)
    return {name: index for index, name in enumerate(names)}


def _row(cells: list[str], columns: dict[str, int], line: int, max_guests: int) -> PmsStayRow:
    if len(cells) != len(columns):
        raise ReportRejectedError("row_shape", line)
    values = {name: cells[index].strip() for name, index in columns.items()}
    if any(not values[name] for name in REQUIRED_COLUMNS):
        raise ReportRejectedError("empty_cell", line)
    if len(values["room"]) > MAX_ROOM_NUMBER_LENGTH:
        raise ReportRejectedError("room", line)
    if len(values["confirmation_no"]) > MAX_BOOKING_REF_LENGTH:
        raise ReportRejectedError("booking_ref", line)
    arrival = _date(values["arrival"], line)
    departure = _date(values["departure"], line)
    if departure < arrival:
        raise ReportRejectedError("dates_order", line)
    return PmsStayRow(
        room_number=values["room"],
        booking_ref=values["confirmation_no"],
        arrival=arrival,
        departure=departure,
        guests_count=_guests(values.get(OPTIONAL_COLUMN, ""), line, max_guests),
    )


def _date(value: str, line: int) -> date:
    try:
        if _ISO_DATE.fullmatch(value):
            return date.fromisoformat(value)
        dotted = _DOTTED_DATE.fullmatch(value)
        if dotted:
            day, month, year = (int(part) for part in dotted.groups())
            return date(year, month, day)
    except ValueError:
        pass  # 30.02 и подобное — та же причина, что неверный формат
    raise ReportRejectedError("date", line)


def _guests(value: str, line: int, max_guests: int) -> int | None:
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= max_guests:
        raise ReportRejectedError("guests", line)
    return int(value)


def _availability_columns(header: list[str]) -> dict[str, int]:
    names = [name.strip().lower() for name in header]
    if len(set(names)) != len(names):
        raise ReportRejectedError("header", 1)
    if sorted(names) != sorted(AVAILABILITY_COLUMNS):
        raise ReportRejectedError("columns", 1)
    return {name: index for index, name in enumerate(names)}


def _night(cells: list[str], columns: dict[str, int], line: int) -> tuple[date, int]:
    if len(cells) != len(columns):
        raise ReportRejectedError("row_shape", line)
    values = {name: cells[index].strip() for name, index in columns.items()}
    if any(not value for value in values.values()):
        raise ReportRejectedError("empty_cell", line)
    night = _date(values["date"], line)
    free_rooms = values["free_rooms"]
    # `[0-9]`, а не `isdigit()`: арабские и прочие цифры Unicode — не число Opera.
    if not _WHOLE_NUMBER.fullmatch(free_rooms) or not (
        MIN_FREE_ROOMS <= int(free_rooms) <= MAX_FREE_ROOMS
    ):
        raise ReportRejectedError("free_rooms", line)
    return night, int(free_rooms)
