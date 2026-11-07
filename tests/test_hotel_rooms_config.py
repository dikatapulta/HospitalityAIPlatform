"""Список номеров отеля в конфиге тенанта и в онбординге (spec 0061 §2, §9).

Схема — последний рубеж: номер с пробелом по краю не совпал бы ни с одним
проживанием, повтор нарисовал бы одну комнату двумя клетками. Онбординг —
правило room service: профиль без ключа список сохраняет, `[]` — очищает.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hospitality.platform.config import HotelProfile, TenantConfig
from hospitality.platform.hotel_rooms import MAX_HOTEL_ROOMS, HotelRoom
from hospitality.tools.onboard_tenant import (
    OnboardingError,
    OnboardingResult,
    TenantProfile,
    build_config,
    format_report,
    load_profile,
)


def _profile(**overrides: Any) -> TenantProfile:
    return TenantProfile.model_validate(
        {
            "city": "Almaty",
            "country_code": "KZ",
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "request_reminder_after_minutes": 30,
            "categories": [{"key": "housekeeping", "name": "Уборка"}],
            **overrides,
        }
    )


def _config(rooms: list[dict[str, Any]]) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile": HotelProfile(city="Almaty", country_code="KZ"),
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "hotel_rooms": rooms,
        }
    )


def test_hotel_without_list_has_no_rooms() -> None:
    assert _config([]).hotel_rooms == ()


def test_room_keeps_order_and_type() -> None:
    config = _config([{"number": "1402", "room_type": "PRMT"}, {"number": "1401"}])

    assert config.hotel_rooms == (
        HotelRoom(number="1402", room_type="PRMT"),
        HotelRoom(number="1401"),
    )


@pytest.mark.parametrize(
    "room",
    [
        {"number": ""},
        {"number": " 1401"},
        {"number": "1401 "},
        {"number": "1" * 21},
        {"number": "1401", "room_type": ""},
        {"number": "1401", "room_type": "X" * 13},
        {"number": "1401", "floor": 14},
    ],
)
def test_room_shape_is_checked(room: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _config([room])


def test_room_numbers_are_unique() -> None:
    with pytest.raises(ValidationError, match="duplicate hotel room number"):
        _config([{"number": "1401"}, {"number": "1401", "room_type": "STDT"}])


def test_room_list_has_a_ceiling() -> None:
    rooms = [{"number": str(10_000 + index)} for index in range(MAX_HOTEL_ROOMS)]
    assert len(_config(rooms).hotel_rooms) == MAX_HOTEL_ROOMS

    with pytest.raises(ValidationError, match="too many hotel rooms"):
        _config([*rooms, {"number": "99999"}])


def test_build_config_keeps_previous_list_without_key_and_clears_on_empty() -> None:
    """`None` — прежний список (публичный профиль номеров пилота не несёт), `[]` — очистить."""
    rooms = [{"number": "1401", "room_type": "FAMR"}, {"number": "1402"}]
    previous = build_config(_profile(hotel_rooms=rooms), reception_phone=None, previous=None)
    assert [room.number for room in previous.hotel_rooms] == ["1401", "1402"]

    kept = build_config(_profile(), reception_phone=None, previous=previous)
    cleared = build_config(_profile(hotel_rooms=[]), reception_phone=None, previous=previous)

    assert kept.hotel_rooms == previous.hotel_rooms
    assert cleared.hotel_rooms == ()


def test_profile_error_is_text_not_traceback(tmp_path: Path) -> None:
    path = tmp_path / "hotel.json"
    path.write_text(
        '{"city": "Almaty", "country_code": "KZ", "timezone": "Asia/Almaty",'
        ' "default_language": "ru", "request_reminder_after_minutes": 30,'
        ' "categories": [{"key": "housekeeping", "name": "Уборка"}],'
        ' "hotel_rooms": [{"number": "1401"}, {"number": "1401"}]}',
        encoding="utf-8",
    )

    with pytest.raises(OnboardingError, match="duplicate hotel room number"):
        load_profile(path)


def test_report_counts_rooms() -> None:
    config = build_config(
        _profile(hotel_rooms=[{"number": "1401"}, {"number": "1402"}]),
        reception_phone=None,
        previous=None,
    )
    report = format_report(
        "rooms-hotel",
        OnboardingResult(
            tenant_id=uuid.uuid4(),
            tenant_created=False,
            categories_created=(),
            categories_existing=("housekeeping",),
            categories_extra=(),
            config=config,
        ),
    )

    assert "Номеров отеля в списке: 2" in report
