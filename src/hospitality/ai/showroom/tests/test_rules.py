"""Правила без модели: короткая команда → команды камеры (spec 0069 §7 п. 4)."""

from __future__ import annotations

import pytest

from hospitality.ai.showroom.rules import match_rules
from hospitality.ai.tools.navigate_showroom import (
    Command,
    FocusFloor,
    FocusRoom,
    Reset,
    SetView,
    StepFloor,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("этаж 15", [FocusFloor(floor=15)]),
        ("Покажи 15-й этаж", [FocusFloor(floor=15)]),
        ("floor 15", [FocusFloor(floor=15)]),
        ("15th floor, please", [FocusFloor(floor=15)]),
        ("15-қабат", [FocusFloor(floor=15)]),
        ("номер 1507", [FocusRoom(room="1507")]),
        ("room 1507", [FocusRoom(room="1507")]),
        ("1507 бөлме", [FocusRoom(room="1507")]),
        ("горы", [SetView(preset="mountains")]),
        ("Вид на горы", [SetView(preset="mountains")]),
        ("mountains", [SetView(preset="mountains")]),
        ("тау", [SetView(preset="mountains")]),
        ("город", [SetView(preset="city")]),
        ("city view", [SetView(preset="city")]),
        ("қала", [SetView(preset="city")]),
        ("сверху", [SetView(preset="top")]),
        ("top", [SetView(preset="top")]),
        ("жоғарыдан", [SetView(preset="top")]),
        ("с улицы", [SetView(preset="street")]),
        ("street", [SetView(preset="street")]),
        ("көшеден", [SetView(preset="street")]),
        ("выше", [StepFloor(direction="up")]),
        ("ниже", [StepFloor(direction="down")]),
        ("up", [StepFloor(direction="up")]),
        ("төмен", [StepFloor(direction="down")]),
        ("сначала", [Reset()]),
        ("reset", [Reset()]),
        ("басынан", [Reset()]),
        ("этаж 15, вид на горы", [FocusFloor(floor=15), SetView(preset="mountains")]),
    ],
)
def test_short_command_needs_no_model(text: str, expected: list[Command]) -> None:
    assert match_rules(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Слово «горы» есть, но смысл — подбор номеров: решает модель.
        "Высокий этаж с видом на горы, двенадцать номеров рядом",
        "с 12 по 15 марта, двое, вид на горы",
        "mountain view rooms for 12 people",
        "12 номеров",
        "покажи зал для банкета",
        "қалаймын",  # «хочу», а не «қала» (город)
        "тауар",  # «товар», а не «тау» (горы)
        "",
        "   ",
        "этаж",
    ],
)
def test_anything_else_goes_to_the_model(text: str) -> None:
    assert match_rules(text) is None
