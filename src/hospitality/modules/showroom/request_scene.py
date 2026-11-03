"""Что запрос берёт из сцены отеля (spec 0069 §2.1, §2.4): номера, виды, типы номеров.

Форма вправе назвать только номер, вид и тип, которые есть в сцене (422
`ERR-SHOWROOM-004` иначе), а карточка службы — показать их названия. Остального
сцена запросу не даёт: ни этажей, ни сторон, ни ориентира цены.

Протоколы ниже — подмножество `SceneDocument` (`scene.py`): запрос зависит только от
этих трёх словарей.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from hospitality.modules.showroom.service import get_scene
from hospitality.platform.config import LocalizedText


class _Named(Protocol):
    @property
    def names(self) -> LocalizedText: ...


class _Room(Protocol):
    @property
    def number(self) -> str: ...


class _Floor(Protocol):
    @property
    def rooms(self) -> Sequence[_Room]: ...


class SceneLike(Protocol):
    """Подмножество `SceneDocument` (§2.1), нужное запросу."""

    @property
    def floors(self) -> Sequence[_Floor]: ...

    @property
    def views(self) -> Mapping[str, _Named]: ...

    @property
    def room_types(self) -> Mapping[str, _Named]: ...


@dataclass(frozen=True)
class SceneFacts:
    room_numbers: frozenset[str]
    views: Mapping[str, LocalizedText]
    room_types: Mapping[str, LocalizedText]


def scene_facts(scene: SceneLike) -> SceneFacts:
    return SceneFacts(
        room_numbers=frozenset(room.number for floor in scene.floors for room in floor.rooms),
        views={key: view.names for key, view in scene.views.items()},
        room_types={key: room_type.names for key, room_type in scene.room_types.items()},
    )


async def load_scene_facts() -> SceneFacts | None:
    """Сцена отеля (внутри `tenant_context`); сцены нет — None."""
    current = await get_scene()
    return scene_facts(current.scene) if current is not None else None
