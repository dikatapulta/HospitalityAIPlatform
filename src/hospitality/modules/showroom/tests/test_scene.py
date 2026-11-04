"""Схема сцены витрины (spec 0069 §2.1, §7 п. 1): отказ — 422 `ERR-SHOWROOM-002`
с путём и причиной; помещение с вместимостью проходит."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from hospitality.modules.showroom import api as showroom_api
from hospitality.shared.errors import AppError


def minimal_scene() -> dict[str, Any]:
    """Пять этажей, два номера на третьем и зал на втором."""
    names = {"en": "x"}
    return {
        "building": {"floors_total": 5, "length_m": 40, "width_m": 16, "orientation_deg": 90},
        "sides": {"south": {"names": names, "view_keys": ["mountains"]}, "north": {"names": names}},
        "views": {"mountains": {"names": names, "azimuth_deg": 180}},
        "room_types": {"standard": {"names": names}},
        "floors": [
            {"number": 1, "kind": "lobby"},
            {
                "number": 2,
                "kind": "restaurant",
                "spaces": [
                    {
                        "key": "alatau",
                        "kind": "hall",
                        "names": names,
                        "capacity": {"banquet": 120, "theatre": 200},
                    }
                ],
            },
            {
                "number": 3,
                "kind": "rooms",
                "sellable": True,
                "rooms": [
                    {"number": "301", "side": "south", "type_key": "standard"},
                    {"number": "302", "side": "north", "type_key": "standard"},
                ],
            },
        ],
    }


def _rejected(document: dict[str, Any]) -> str:
    with pytest.raises(AppError) as caught:
        showroom_api.parse_scene(json.dumps(document))
    assert caught.value.code == "ERR-SHOWROOM-002"
    assert caught.value.status_code == 422
    return caught.value.message


def test_minimal_scene_with_hall_is_accepted() -> None:
    scene = showroom_api.parse_scene(json.dumps(minimal_scene()))
    assert [room.number for _, room in scene.rooms()] == ["301", "302"]
    assert scene.floors[1].spaces[0].capacity == {"banquet": 120, "theatre": 200}


def test_extra_field_is_rejected_with_path() -> None:
    document = minimal_scene()
    document["floors"][2]["rooms"][0]["occupied"] = True
    assert _rejected(document).startswith("floors.2.rooms.0.occupied")


def test_duplicate_room_number_is_rejected() -> None:
    document = minimal_scene()
    document["floors"][2]["rooms"][1]["number"] = "301"
    message = _rejected(document)
    assert message.startswith("floors.2.rooms.1.number")
    assert "повторяется" in message


def test_duplicate_space_key_is_rejected() -> None:
    document = minimal_scene()
    document["floors"][0]["spaces"] = copy.deepcopy(document["floors"][1]["spaces"])
    assert _rejected(document).startswith("floors.1.spaces.0.key")


def test_floor_above_floors_total_is_rejected() -> None:
    document = minimal_scene()
    document["floors"][2]["number"] = 6
    assert "выше floors_total" in _rejected(document)


def test_unknown_type_key_is_rejected() -> None:
    document = minimal_scene()
    document["floors"][2]["rooms"][0]["type_key"] = "lux"
    assert _rejected(document).startswith("floors.2.rooms.0.type_key")


def test_side_missing_from_sides_is_rejected() -> None:
    document = minimal_scene()
    document["floors"][2]["rooms"][0]["side"] = "east"
    assert _rejected(document).startswith("floors.2.rooms.0.side")


def test_unknown_view_key_is_rejected() -> None:
    document = minimal_scene()
    document["sides"]["north"]["view_keys"] = ["sea"]
    assert _rejected(document).startswith("sides.north.view_keys.0")


def test_sellable_office_floor_is_rejected() -> None:
    document = minimal_scene()
    document["floors"][0] = {"number": 1, "kind": "offices", "sellable": True}
    assert _rejected(document).startswith("floors.0.sellable")


def test_oversized_file_is_rejected_before_parsing() -> None:
    raw = " " * (showroom_api.MAX_SCENE_BYTES + 1)
    with pytest.raises(AppError) as caught:
        showroom_api.parse_scene(raw)
    assert caught.value.code == "ERR-SHOWROOM-002"
    assert caught.value.message.startswith("(файл)")


def test_not_json_names_the_line() -> None:
    with pytest.raises(AppError) as caught:
        showroom_api.parse_scene('{\n"building": ')
    assert "(файл, строка 2)" in caught.value.message


def test_match_rooms_filters_and_orders_top_down() -> None:
    document = minimal_scene()
    document["floors"].append(
        {
            "number": 4,
            "kind": "rooms",
            "rooms": [
                {"number": "402", "side": "south", "type_key": "standard"},
                {"number": "401", "side": "north", "type_key": "standard"},
            ],
        }
    )
    scene = showroom_api.parse_scene(json.dumps(document))
    query = showroom_api.RoomQuery
    numbers = [m.number for m in showroom_api.match_rooms(scene, query())]
    assert numbers == ["401", "402", "301", "302"]
    by_view = showroom_api.match_rooms(scene, query(view_key="mountains"))
    assert [m.number for m in by_view] == ["402", "301"]
    assert by_view[0].view_keys == ("mountains",)
    assert [m.number for m in showroom_api.match_rooms(scene, query(floor_to=3))] == ["301", "302"]
    assert [m.number for m in showroom_api.match_rooms(scene, query(side="north", count=1))] == [
        "401"
    ]
    assert showroom_api.match_rooms(scene, query(type_key="suite")) == []


def test_landmark_forms_and_neighbours_are_accepted() -> None:
    document = minimal_scene()
    names = {"en": "x"}
    document["views"]["mountains"]["landmark"] = "ridge"
    document["views"]["tv"] = {
        "names": names,
        "azimuth_deg": 140,
        "distance_m": 2300,
        "landmark": "hill_tower",
    }
    document["building"]["neighbours"] = [
        {"kind": "street", "names": names, "azimuth_deg": 270, "distance_m": 90},
        {
            "kind": "building",
            "names": names,
            "azimuth_deg": 138,
            "distance_m": 190,
            "length_m": 99,
            "width_m": 129,
            "height_m": 22,
        },
    ]
    scene = showroom_api.parse_scene(json.dumps(document))
    assert scene.views["tv"].landmark == "hill_tower"
    assert [n.kind for n in scene.building.neighbours] == ["street", "building"]
    plain = showroom_api.parse_scene(json.dumps(minimal_scene()))
    assert plain.views["mountains"].landmark is None
    assert plain.building.neighbours == ()


def test_unknown_landmark_is_rejected() -> None:
    document = minimal_scene()
    document["views"]["mountains"]["landmark"] = "volcano"
    assert _rejected(document).startswith("views.mountains.landmark")


def test_far_neighbour_is_rejected() -> None:
    document = minimal_scene()
    document["building"]["neighbours"] = [
        {"kind": "building", "names": {"en": "x"}, "azimuth_deg": 0, "distance_m": 2001}
    ]
    assert _rejected(document).startswith("building.neighbours.0.distance_m")
