"""Сводка сцены для промпта помощника витрины (spec 0069 §2.3 п. 2).

Не список 350 номеров, а то, по чему посетитель выбирает: этажи с назначением,
стороны и виды, типы номеров. Поиск номеров по условиям делает код
(`showroom.api.find_rooms`), модели номера знать не нужно. Цен в сводке нет
(`price_note` — только на странице, §2.2): модель не может назвать то, чего не видела.

Сегодняшняя дата — в поясе отеля (FOUNDATION §9, образец `ai/stay_extension.py`):
`draft_request` требует полной даты, и без «сегодня» модель угадывала бы год
(находка ревью PR #801).
"""

from __future__ import annotations

from datetime import date
from typing import Final

from hospitality.ai.tools.navigate_showroom import Audience
from hospitality.modules.showroom.api import Floor, SceneDocument

_WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_SIDES: Final = ("north", "east", "south", "west")
_AUDIENCE: Final[dict[Audience, str]] = {
    "group": "a travel agent or a company choosing rooms for a group (draft_request kind: group)",
    "booking": "a private guest choosing dates and a room (draft_request kind: booking)",
}


def render_summary(scene: SceneDocument, *, audience: Audience, today: date) -> str:
    """Блок системного промпта: сегодня, посетитель, здание."""
    lines = [
        "\n\n# This visit\n",
        f"Today at the hotel is {_day(today)}. The visitor is {_AUDIENCE[audience]}.",
        "\n# Building\n",
        f"{scene.building.floors_total} floors.",
    ]
    sides = [
        f"{key} — {info.names.en}"
        + (f" (views: {', '.join(info.view_keys)})" if info.view_keys else "")
        for key in _SIDES
        if (info := scene.sides.get(key)) is not None
    ]
    if sides:
        lines.append("Sides: " + "; ".join(sides) + ".")
    if scene.views:
        lines.append(
            "Views: "
            + "; ".join(f"{key} — {view.names.en}" for key, view in scene.views.items())
            + "."
        )
    if scene.room_types:
        lines.append(
            "Room types: "
            + "; ".join(
                f"{key} — {info.names.en}"
                + (f", {info.windows} windows" if info.windows > 1 else "")
                + (f" ({info.note.en})" if info.note else "")
                for key, info in scene.room_types.items()
            )
            + "."
        )
    lines.append("Floors:")
    lines.extend(_floor_lines(scene.floors))
    return "\n".join(lines)


def _floor_lines(floors: tuple[Floor, ...]) -> list[str]:
    """Соседние этажи с одинаковым описанием — одной строкой: «3–20: rooms …»."""
    groups: list[tuple[int, int, str]] = []
    for floor in sorted(floors, key=lambda item: item.number):
        text = _describe(floor)
        if groups and groups[-1][2] == text and groups[-1][1] == floor.number - 1:
            groups[-1] = (groups[-1][0], floor.number, text)
        else:
            groups.append((floor.number, floor.number, text))
    return [
        f"- {first}: {text}" if first == last else f"- {first}–{last}: {text}"
        for first, last, text in groups
    ]


def _describe(floor: Floor) -> str:
    parts: list[str] = [floor.kind]
    if floor.label is not None:
        parts.append(f'"{floor.label.en}"')
    if floor.rooms:
        by_side = [
            f"{side} {count}"
            for side in _SIDES
            if (count := sum(1 for room in floor.rooms if room.side == side))
        ]
        types = sorted({room.type_key for room in floor.rooms})
        parts.append(f"— {', '.join(by_side)} rooms; types: {', '.join(types)}")
    if floor.rooms and not floor.sellable:
        parts.append("(not for sale)")
    for space in floor.spaces:
        capacity = ", ".join(f"{layout} {people}" for layout, people in space.capacity.items())
        parts.append(f'· {space.kind} "{space.names.en}"' + (f" ({capacity})" if capacity else ""))
    return " ".join(parts)


def _day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]}, {day.day} {_MONTHS[day.month - 1]} {day.year}"
