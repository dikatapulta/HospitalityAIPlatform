"""Сцена 3D-витрины — здание отеля числами (spec 0069 §2.1).

Один документ JSON на тенанта: корпус, стороны и виды, типы номеров, этажи с
номерами и помещениями. Просмотрщик строит здание из этих чисел — файла модели нет.
Имён, занятости и цен из PMS в схеме нет и быть не может; единственное число о
деньгах — `price_note` типа номера, ориентир, который задал сам отель.

`parse_scene` — единственный вход сцены снаружи (загрузка в кабинете, демо-сид):
размер, JSON, схема, затем ссылки между частями. Отказ — 422 `ERR-SHOWROOM-002` с
путём и причиной: «floors.3.rooms.2.type_key — нет такого типа номера».
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hospitality.platform.config_values import LocalizedText
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

ERR_SHOWROOM_SCENE_INVALID: Final = "ERR-SHOWROOM-002"

MAX_SCENE_BYTES: Final = 512 * 1024
MAX_SCENE_ROOMS: Final = 2000
MAX_FLOORS: Final = 60
_KEY: Final = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_ROOM_NUMBER: Final = r"^[0-9A-Za-z][0-9A-Za-z-]{0,9}$"

SideKey = Literal["north", "east", "south", "west"]
SIDE_KEYS: Final[tuple[SideKey, ...]] = ("north", "east", "south", "west")
FloorKind = Literal["rooms", "lobby", "restaurant", "offices", "technical", "monthly", "closed"]
SpaceKind = Literal["hall", "restaurant", "lobby", "spa", "office", "other"]
Seating = Literal["theatre", "banquet", "classroom", "u_shape", "reception"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Building(_Strict):
    """Корпус — прямоугольник; стороны считаются от азимута длинной оси."""

    floors_total: int = Field(ge=1, le=MAX_FLOORS)
    length_m: float = Field(gt=0, le=500)
    width_m: float = Field(gt=0, le=500)
    orientation_deg: int = Field(ge=0, lt=360)
    rounded_ends: bool = False
    facade_step: bool = False
    podium_floors: int = Field(default=0, ge=0, le=MAX_FLOORS)
    crown: bool = False


class SideInfo(_Strict):
    names: LocalizedText
    view_keys: tuple[str, ...] = ()


class ViewInfo(_Strict):
    names: LocalizedText
    azimuth_deg: int | None = Field(default=None, ge=0, lt=360)
    distance_m: int | None = Field(default=None, ge=0)


class RoomTypeInfo(_Strict):
    names: LocalizedText
    windows: Literal[1, 2] = 1
    note: LocalizedText | None = None
    # Ориентир отеля («от 45 000 ₸ за ночь»); нет — «уточнит отдел бронирования».
    price_note: LocalizedText | None = None


class Room(_Strict):
    number: str = Field(pattern=_ROOM_NUMBER)
    side: SideKey
    type_key: str
    view_keys: tuple[str, ...] = ()


class Space(_Strict):
    """Помещение не-номер (банкетный зал): формат на вырост, просмотрщик лишь подписывает."""

    key: str = Field(pattern=_KEY)
    kind: SpaceKind
    names: LocalizedText
    area_m2: float | None = Field(default=None, gt=0)
    capacity: dict[Seating, int] = Field(default_factory=dict)


class Floor(_Strict):
    number: int = Field(ge=1, le=MAX_FLOORS)
    kind: FloorKind
    sellable: bool = False
    label: LocalizedText | None = None
    rooms: tuple[Room, ...] = ()
    spaces: tuple[Space, ...] = ()


class SceneDocument(_Strict):
    schema_version: Literal[1] = 1
    building: Building
    sides: dict[SideKey, SideInfo]
    views: dict[str, ViewInfo]
    room_types: dict[str, RoomTypeInfo]
    floors: tuple[Floor, ...]

    def rooms(self) -> list[tuple[Floor, Room]]:
        """Все номера сцены с их этажом — в порядке документа."""
        return [(floor, room) for floor in self.floors for room in floor.rooms]


class SceneError(ValueError):
    """Сцена не принята: где (`path`) и почему (`reason`)."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path} — {reason}")
        self.path = path
        self.reason = reason


def parse_scene(raw: str | bytes) -> SceneDocument:
    """Сцена из текста файла: размер, JSON, схема, ссылки. Отказ — 422 `002`."""
    try:
        return _parse(raw)
    except SceneError as error:
        logger.warning(
            "showroom_scene_rejected",
            error_code=ERR_SHOWROOM_SCENE_INVALID,
            path=error.path,
            reason=error.reason,
        )
        raise AppError(
            code=ERR_SHOWROOM_SCENE_INVALID, message=str(error), status_code=422
        ) from error


def _parse(raw: str | bytes) -> SceneDocument:
    data = raw.encode() if isinstance(raw, str) else raw
    if len(data) > MAX_SCENE_BYTES:
        raise SceneError("(файл)", f"больше {MAX_SCENE_BYTES // 1024} КБ")
    try:
        document = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        line = getattr(error, "lineno", None)
        where = f"(файл, строка {line})" if line else "(файл)"
        raise SceneError(where, "не JSON") from error
    try:
        scene = SceneDocument.model_validate(document)
    except ValidationError as error:
        first = error.errors()[0]
        path = ".".join(str(part) for part in first["loc"]) or "(сцена)"
        raise SceneError(path, first["msg"]) from error
    check_references(scene)
    return scene


def check_references(scene: SceneDocument) -> None:
    """Ссылки между частями сцены: ключи из словарей, уникальность, пределы этажей."""
    _check_keys("views", scene.views)
    _check_keys("room_types", scene.room_types)
    for side_key, side in scene.sides.items():
        for index, view_key in enumerate(side.view_keys):
            if view_key not in scene.views:
                raise SceneError(f"sides.{side_key}.view_keys.{index}", f"нет вида {view_key!r}")
    floors_total = scene.building.floors_total
    if scene.building.podium_floors > floors_total:
        raise SceneError("building.podium_floors", "больше floors_total")
    seen_floors: set[int] = set()
    seen_rooms: set[str] = set()
    seen_spaces: set[str] = set()
    for f_index, floor in enumerate(scene.floors):
        where = f"floors.{f_index}"
        if floor.number > floors_total:
            raise SceneError(f"{where}.number", f"этаж {floor.number} выше floors_total")
        if floor.number in seen_floors:
            raise SceneError(f"{where}.number", f"этаж {floor.number} повторяется")
        seen_floors.add(floor.number)
        if floor.sellable and floor.kind != "rooms":
            raise SceneError(f"{where}.sellable", "продаваемым бывает только этаж kind=rooms")
        for r_index, room in enumerate(floor.rooms):
            _check_room(scene, room, f"{where}.rooms.{r_index}", seen_rooms)
        for s_index, space in enumerate(floor.spaces):
            if space.key in seen_spaces:
                raise SceneError(f"{where}.spaces.{s_index}.key", f"{space.key!r} повторяется")
            seen_spaces.add(space.key)
    if len(seen_rooms) > MAX_SCENE_ROOMS:
        raise SceneError("floors", f"номеров больше {MAX_SCENE_ROOMS}")


def _check_keys(where: str, values: Mapping[str, object]) -> None:
    for key in values:
        if not re.fullmatch(_KEY, key):
            raise SceneError(f"{where}.{key}", "ключ — латиница, цифры, «-» и «_», до 32 знаков")


def _check_room(scene: SceneDocument, room: Room, where: str, seen: set[str]) -> None:
    if room.number in seen:
        raise SceneError(f"{where}.number", f"номер {room.number} повторяется")
    seen.add(room.number)
    if room.side not in scene.sides:
        raise SceneError(f"{where}.side", f"нет стороны {room.side!r} в sides")
    if room.type_key not in scene.room_types:
        raise SceneError(f"{where}.type_key", f"нет типа номера {room.type_key!r}")
    for v_index, view_key in enumerate(room.view_keys):
        if view_key not in scene.views:
            raise SceneError(f"{where}.view_keys.{v_index}", f"нет вида {view_key!r}")
