"""Схемы модуля showroom на границе (spec 0069 §4, R-6)."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.showroom.scene import MAX_FLOORS, SceneDocument, SideKey

# Номеров в одном подборе — не больше (§2.3: `FindRooms.count` 1..60).
MAX_ROOMS_PER_QUERY: Final = 60
MAX_KEY_LENGTH: Final = 32


class SceneRead(BaseModel):
    """Сцена отеля и её версия: растёт с каждой загрузкой."""

    model_config = ConfigDict(frozen=True)

    scene: SceneDocument
    scene_version: int
    replaced_at: datetime


class RoomQuery(BaseModel):
    """Условия подбора — панель на странице и команда помощника `FindRooms` (P-5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    floor_from: int | None = Field(default=None, ge=1, le=MAX_FLOORS)
    floor_to: int | None = Field(default=None, ge=1, le=MAX_FLOORS)
    side: SideKey | None = None
    view_key: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    type_key: str | None = Field(default=None, max_length=MAX_KEY_LENGTH)
    count: int | None = Field(default=None, ge=1, le=MAX_ROOMS_PER_QUERY)


class RoomMatch(BaseModel):
    """Номер, подошедший под условия: где он и что из него видно."""

    model_config = ConfigDict(frozen=True)

    number: str
    floor: int
    side: SideKey
    type_key: str
    view_keys: tuple[str, ...]
