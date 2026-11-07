"""Список номеров отеля для доски «Номера» (spec 0061 §2, issues #666, #674).

Свободный номер нигде больше не хранится: в проживаниях — только занятые, в
путях после выезда — только убираемые. Поле `TenantConfig.hotel_rooms`;
отдельный файл, а не ещё один класс в `config.py`: тот за границей R-3.

Этаж не хранится — его выводит `housekeeping.api.floor_of_room` (spec 0053 §3):
один владелец факта, доска и раздача этажей не разойдутся. Сторона коридора —
подпись ряда этажа, как на румплане отеля («Вид на горы»): текст профиля, а не
перечисление в коде (NG-7). Номера, типы и стороны пилота — данные отеля, они
только в файле профиля `*.local`.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# Предел списка: больше — опечатка в файле профиля, а не отель (у пилота ~350).
MAX_HOTEL_ROOMS: Final = 2000
# Длина номера — как у `stays.room_number`: номер проживания обязан влезть в список.
MAX_ROOM_NUMBER_LENGTH: Final = 20
# Код типа из Opera («STDT», «SUPD») — короткий, на плитке и в панели номера.
MAX_ROOM_TYPE_LENGTH: Final = 12
# Подпись стороны коридора — короткая: стоит слева от ряда клеток этажа.
MAX_ROOM_SIDE_LENGTH: Final = 40
# Без крайних пробелов: совпадение с номером проживания — строка в строку (§3).
_TRIMMED: Final = r"^\S(.*\S)?$"


class HotelRoom(BaseModel):
    """Номер отеля: как его вводят при заселении, тип из Opera и сторона коридора
    (если известны). Номера одной стороны встают на доске одним рядом этажа."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str = Field(min_length=1, max_length=MAX_ROOM_NUMBER_LENGTH, pattern=_TRIMMED)
    room_type: str | None = Field(
        default=None, min_length=1, max_length=MAX_ROOM_TYPE_LENGTH, pattern=_TRIMMED
    )
    side: str | None = Field(
        default=None, min_length=1, max_length=MAX_ROOM_SIDE_LENGTH, pattern=_TRIMMED
    )


def ensure_valid_hotel_rooms(rooms: tuple[HotelRoom, ...]) -> tuple[HotelRoom, ...]:
    """Номера не повторяются и их не больше `MAX_HOTEL_ROOMS`: повтор нарисовал бы
    одну комнату двумя клетками с разными статусами."""
    if len(rooms) > MAX_HOTEL_ROOMS:
        raise ValueError(f"too many hotel rooms: {len(rooms)} > {MAX_HOTEL_ROOMS}")
    seen: set[str] = set()
    for room in rooms:
        if room.number in seen:
            raise ValueError(f"duplicate hotel room number: {room.number!r}")
        seen.add(room.number)
    return rooms
