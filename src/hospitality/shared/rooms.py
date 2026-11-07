"""Этаж номера комнаты (spec 0053 §3) — одно правило в двух формах: Python и SQL.

Номер из 3–4 ASCII-цифр — этаж без двух последних (704 → 7, 1204 → 12); этаж —
от 1 до 99. Иначе этажа нет: «Люкс», «12», «A-704», «050». Python-форма зовёт
адресацию хаускипинга (`housekeeping_api.floor_of_room`), SQL-форма режет ленты
заявок по этажам в базе до среза `limit` (spec 0053 §2, #856). Две формы — одно
правило (P-12): их равенство на одном наборе номеров держит тест
`modules/requests/tests/test_floor_scope.py`, правишь одну — правь другую.
"""

from __future__ import annotations

import re
from typing import Final

from sqlalchemy import Integer, SQLColumnExpression, case, cast, func
from sqlalchemy.sql.elements import ColumnElement

FLOOR_MIN: Final = 1
FLOOR_MAX: Final = 99

# Только ASCII-цифры: `str.isdigit` пропустил бы «²» и арабские цифры.
_ROOM_WITH_FLOOR: Final = re.compile(r"[0-9]{3,4}")
# То же для Postgres: `~` — регулярное выражение, `[0-9]` — тоже только ASCII.
_ROOM_WITH_FLOOR_SQL: Final = "^[0-9]{3,4}$"


def floor_of_room(room_number: str | None) -> int | None:
    """Этаж номера комнаты; None — этажа нет («Люкс», «12», «050»)."""
    if room_number is None:
        return None
    room = room_number.strip()
    if _ROOM_WITH_FLOOR.fullmatch(room) is None:
        return None
    floor = int(room) // 100
    return floor if FLOOR_MIN <= floor <= FLOOR_MAX else None


def floor_of_room_sql(room_number: SQLColumnExpression[str | None]) -> ColumnElement[int | None]:
    """Тот же этаж выражением SQL; NULL — этажа нет.

    Пробелы по краям снимает `btrim` — как `strip()` у номера, вписанного гостем
    или моделью. `CASE` вложенный, а не `AND`: Postgres не обещает порядок
    вычисления `AND`, и приведение «Люкс» к числу упало бы раньше проверки.
    Верхней границы проверять не нужно: из четырёх цифр выходит не больше 99.
    """
    room = func.btrim(room_number)
    number = cast(room, Integer)
    return case(
        (
            room.regexp_match(_ROOM_WITH_FLOOR_SQL),
            case((number >= FLOOR_MIN * 100, number // 100), else_=None),
        ),
        else_=None,
    )
