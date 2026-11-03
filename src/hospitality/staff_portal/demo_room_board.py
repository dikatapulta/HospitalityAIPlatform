"""Данные демо-страницы «Номера» (issue #603) — к показу руководству отеля 05.11.

Доска всех номеров отеля со статусом работы — не официальным статусом OPERA
(spec 0046 §2) — и шагами уборки. Номера, стороны коридора и типы — по румпланам
отеля (`demo_room_plan.py`, #674); статусы, гости и время выдуманы здесь;
страница лишь переводит номер по шагам в браузере (`static/demo_room_board.js`):
ни БД, ни событий, ни сохранения — перезагрузка возвращает исходную картину.
Настоящая доска ждёт порта проживаний (#122).

Шаги и надписи кнопок — те же, что у живой доски «Выезды и уборка»
(`turnovers._row`, spec 0046 §3): показ обязан совпадать с настоящей страницей.
`STEPS` — единственная таблица переходов: шаблон рисует из неё кнопки с
`data-to`, JS только читает атрибуты и своих переходов не знает.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from hospitality.platform.config import DEFAULT_STAY_CHECKOUT_HOUR
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal.demo_room_plan import FLOORS, PLAN

StatusKey = Literal[
    "occupied", "due_out", "reported", "inspected", "cleaning", "cleaned", "ready", "vacant"
]
# Что шаг делает с замечанием хаускипинга: «Замечание» пишет, «Проверено» —
# проверено без замечаний, «Принять» закрывает путь номера.
NoteMode = Literal["keep", "set", "clear"]

# Путь демо «Забытые вещи» (#604): «Найдена вещь» ведёт туда с `?room=`.
LOST_FOUND_PATH: Final = "/lost-found"


@dataclass(frozen=True)
class Status:
    key: StatusKey
    short: str  # подпись в клетке
    label: str  # подпись в панели номера
    counter: str  # счётчик над доской
    icon: str  # значок — имя макроса `_icons.html`


@dataclass(frozen=True)
class Step:
    action: str  # data-action кнопки
    label: str  # надпись — как на живой доске выездов
    to: StatusKey
    line: str  # начало строки «выезд 10:40» после шага
    style: str = "btn-primary"
    note: NoteMode = "keep"
    remark: bool = False  # открывает поле «Что не так»


STATUSES: Final[tuple[Status, ...]] = (
    Status("occupied", "занят", "Занят", "Заняты", "bed"),
    Status("due_out", "выезд сегодня", "Выезд сегодня", "Выезд сегодня", "logout"),
    Status("reported", "ждёт проверки", "Выехал — ждёт проверки", "Ждут проверки", "search"),
    Status("inspected", "проверен", "Проверен — ждёт уборки", "Проверены", "check"),
    Status("cleaning", "уборка", "Уборка", "Убираются", "broom"),
    Status("cleaned", "ждёт приёмки", "Убран — ждёт приёмки", "Ждут приёмки", "clock"),
    Status("ready", "готов", "Готов к заселению", "Готовы", "key"),
    Status("vacant", "свободен", "Свободен", "Свободны", "door"),
)

_REPORT: Final = Step("report", "Отметить выезд", "reported", "выезд")
STEPS: Final[dict[StatusKey, tuple[Step, ...]]] = {
    "occupied": (_REPORT,),
    "due_out": (_REPORT,),
    "reported": (
        Step("inspect", "Проверено", "inspected", "проверено", note="clear"),
        Step(
            "remark",
            "Замечание",
            "inspected",
            "проверено",
            style="btn-secondary",
            note="set",
            remark=True,
        ),
    ),
    "inspected": (Step("start-cleaning", "Убираю", "cleaning", "уборка с"),),
    "cleaning": (Step("finish-cleaning", "Убрано", "cleaned", "убрано"),),
    "cleaned": (Step("accept", "Принять", "ready", "готов", note="clear"),),
    "ready": (),
    "vacant": (),
}
# Слово строки «выезд 10:40» у номера, пришедшего в статус шагом, — из той же
# таблицы, чтобы начальная картина и нажатые шаги писали одинаково.
_ARRIVAL_LINE: Final[dict[StatusKey, str]] = {
    step.to: step.line for steps in STEPS.values() for step in steps
}

# Начальная раскладка: сколько номеров в каждом статусе, остальные заняты —
# правдоподобный день отеля, где на показе видны все статусы.
_LAYOUT: Final[tuple[tuple[StatusKey, int], ...]] = (
    ("due_out", 9),
    ("reported", 4),
    ("inspected", 3),
    ("cleaning", 6),
    ("cleaned", 4),
    ("ready", 31),
    ("vacant", 17),
)
# Номера раскладываются шагом 37 по кругу из всех номеров: 37 взаимно просто с их
# числом (351 = 3³·13), поэтому обход проходит каждый номер ровно раз, и статусы
# рассыпаны по этажам.
_SPREAD_STEP: Final = 37
# Минут назад у последнего шага: первый номер статуса — раньше всех.
_MINUTES_AGO: Final[dict[StatusKey, int]] = {
    "reported": 15,
    "inspected": 35,
    "cleaning": 50,
    "cleaned": 80,
    "ready": 120,
}
_DEMO_NOTE: Final = "Минибар: 2 колы. Разбит стакан."
# Число гостей (#661) — у тех, кто живёт в номере сейчас: хаускипингу оно нужно
# на воду, полотенца и тапочки по гостю. После «Отметить выезд» гостей нет, и
# любой шаг доски начинается с выезда или идёт после него — шаг число снимает.
GUEST_STATUSES: Final[frozenset[StatusKey]] = frozenset({"occupied", "due_out"})
# Гостей по кругу номеров: чаще двое, иногда один, трое или четверо.
_GUESTS_CYCLE: Final = (2, 1, 2, 3, 2, 1, 2, 4, 1, 2, 3)


@dataclass(frozen=True)
class Room:
    number: str
    floor: int
    room_type: str  # код типа из Opera, как на румплане
    span: int  # клеток ряда: люкс — две-три
    status: StatusKey
    line: str
    note: str = ""
    guests: int = 0  # 0 — в номере никто не живёт


def _plan_rooms() -> list[tuple[int, str, str, int]]:
    """Номера румпланов в порядке доски: этаж, ряд, клетки ряда слева направо."""
    return [
        (floor, cell.number, cell.room_type, cell.span)
        for floor in FLOORS
        for side in PLAN[floor]
        for cell in side.cells
        if cell is not None
    ]


def _line(status: StatusKey, rank: int, local_now: datetime) -> str:
    """Строка под статусом: «Гость до 14.11», «выезд 10:40», «Гостя нет»."""
    if status == "occupied":
        until = local_now.date() + timedelta(days=1 + rank % 9)
        return f"Гость до {until:%d.%m}"
    if status == "due_out":
        return f"Выезд сегодня в {DEFAULT_STAY_CHECKOUT_HOUR}:00"
    if status == "vacant":
        return "Гостя нет"
    moment = local_now - timedelta(minutes=_MINUTES_AGO[status] + 7 * rank)
    return f"{_ARRIVAL_LINE[status]} {moment:%H:%M}"


def build_rooms(local_now: datetime) -> list[Room]:
    """Все номера доски в порядке этажей. `local_now` — время по поясу отеля."""
    numbers = _plan_rooms()
    statuses: dict[int, tuple[StatusKey, int]] = {}
    order = [(index * _SPREAD_STEP) % len(numbers) for index in range(len(numbers))]
    cursor = 0
    for status, count in _LAYOUT:
        for rank in range(count):
            statuses[order[cursor]] = (status, rank)
            cursor += 1
    rooms = []
    occupied = 0
    for index, (floor, number, room_type, span) in enumerate(numbers):
        if index in statuses:
            status, rank = statuses[index]
        else:
            status, rank, occupied = "occupied", occupied, occupied + 1
        note = _DEMO_NOTE if status == "inspected" and rank == 0 else ""
        guests = _GUESTS_CYCLE[index % len(_GUESTS_CYCLE)] if status in GUEST_STATUSES else 0
        line = _line(status, rank, local_now)
        rooms.append(Room(number, floor, room_type, span, status, line, note, guests))
    return rooms


def build_demo_room_board_context(
    staff: StaffContext, local_now: datetime, *, lost_found: bool
) -> dict[str, Any]:
    """Контекст шаблона `demo_room_board.html`: счётчики, этажи рядами румплана
    (клетка — номер или `None`, пустая), лица статусов и кнопки шагов.
    `lost_found` — включено ли демо «Забытые вещи» (#604): без него кнопки
    «Найдена вещь» нет."""
    rooms = build_rooms(local_now)
    counts = Counter(room.status for room in rooms)
    by_number = {room.number: room for room in rooms}
    return {
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "total": len(rooms),
        "statuses": [
            {"status": status, "count": counts[status.key], "steps": STEPS[status.key]}
            for status in STATUSES
        ],
        "status_by_key": {status.key: status for status in STATUSES},
        "floors": [
            {
                "number": floor,
                "sides": [
                    {
                        "key": side.key,
                        "label": side.label,
                        "cells": [by_number[cell.number] if cell else None for cell in side.cells],
                    }
                    for side in PLAN[floor]
                ],
            }
            for floor in FLOORS
        ],
        "lost_found_href": (f"/staff/{staff.tenant_slug}{LOST_FOUND_PATH}" if lost_found else None),
    }
