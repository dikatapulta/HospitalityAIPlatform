"""Инструмент помощника 3D-витрины: команды камеры и черновик запроса (spec 0069 §2.3).

Обёртка по канону `create_service_request`, но без исполнения: инструмент только
описывает, что показать на модели здания, и модель отвечает ТОЛЬКО его аргументами
(`forced_tool`) — свободного текста, который увидел бы посетитель, нет (§7.6).
Класс `auto` (P-9): побочных эффектов нет. `draft_request` лишь открывает на
странице карточку подтверждения — запрос отправляет кнопкой сам посетитель, а
инструмента, создающего запрос, у назначения `showroom` нет (реестр — `SHOWROOM_TOOLS`).

Схема строится под сцену отеля, как enum служб у заявки (§7.4): ключи видов и
типов номеров — только из сцены, этаж — в пределах здания. Остальное (номер
вне сцены, этаж без номеров) отбрасывает `ai/showroom/navigate.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ConfirmationClass

NAME: Final = "navigate_showroom"
CONFIRMATION_CLASS: Final = ConfirmationClass.AUTO
CREATES_REQUEST: Final = False

MAX_COMMANDS: Final = 4
MAX_REPLY_LENGTH: Final = 200
MAX_FOUND_ROOMS: Final = 60
MAX_GROUP_GUESTS: Final = 500
MAX_WISHES_LENGTH: Final = 500
MAX_ROOM_NUMBER_LENGTH: Final = 20

Side = Literal["north", "east", "south", "west"]
ViewPreset = Literal["mountains", "city", "top", "street"]
Audience = Literal["group", "booking"]


class _Command(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class FocusFloor(_Command):
    type: Literal["focus_floor"] = "focus_floor"
    floor: int = Field(ge=1, le=60)


class FocusRoom(_Command):
    type: Literal["focus_room"] = "focus_room"
    room: str = Field(min_length=1, max_length=MAX_ROOM_NUMBER_LENGTH)


class SetView(_Command):
    type: Literal["set_view"] = "set_view"
    preset: ViewPreset


class StepFloor(_Command):
    """«Выше» / «ниже» — от этажа, который сейчас на экране: его знает только страница."""

    type: Literal["step_floor"] = "step_floor"
    direction: Literal["up", "down"]


class Reset(_Command):
    type: Literal["reset"] = "reset"


class FindRooms(_Command):
    type: Literal["find_rooms"] = "find_rooms"
    floor_from: int | None = Field(default=None, ge=1, le=60)
    floor_to: int | None = Field(default=None, ge=1, le=60)
    side: Side | None = None
    view_key: str | None = None
    type_key: str | None = None
    count: int | None = Field(default=None, ge=1, le=MAX_FOUND_ROOMS)


class DraftRequest(_Command):
    """Черновик запроса для карточки подтверждения — не запрос (P-9)."""

    type: Literal["draft_request"] = "draft_request"
    kind: Audience
    guests: int | None = Field(default=None, ge=1, le=MAX_GROUP_GUESTS)
    check_in: date | None = None
    check_out: date | None = None
    room_numbers: tuple[str, ...] = Field(default=(), max_length=MAX_FOUND_ROOMS)
    view_key: str | None = None
    type_key: str | None = None
    wishes: str | None = Field(default=None, max_length=MAX_WISHES_LENGTH)


Command = Annotated[
    FocusFloor | FocusRoom | SetView | StepFloor | Reset | FindRooms | DraftRequest,
    Field(discriminator="type"),
]

_DESCRIPTION: Final = (
    "Move the camera over the hotel's 3D building scheme and answer the visitor. "
    "commands run in order; reply is shown under the model."
)
_REPLY_DESCRIPTION: Final = (
    "One or two short sentences in the visitor's language: what you showed and what they "
    "can clarify. Never prices, availability, discounts or promises."
)


def build_spec(
    *, floors_total: int, view_keys: Sequence[str], type_keys: Sequence[str]
) -> ToolSpec:
    """`ToolSpec` под сцену отеля: виды и типы номеров — enum из сцены (§7.4)."""
    floor: dict[str, Any] = {"type": "integer", "minimum": 1, "maximum": floors_total}
    view_key = _key_schema(view_keys)
    type_key = _key_schema(type_keys)
    commands: list[dict[str, Any]] = [
        _command("focus_floor", {"floor": floor}, required=["floor"]),
        _command("focus_room", {"room": {"type": "string"}}, required=["room"]),
        _command(
            "set_view",
            {"preset": {"type": "string", "enum": ["mountains", "city", "top", "street"]}},
            required=["preset"],
        ),
        _command(
            "step_floor",
            {"direction": {"type": "string", "enum": ["up", "down"]}},
            required=["direction"],
        ),
        _command("reset", {}),
        _command(
            "find_rooms",
            {
                "floor_from": floor,
                "floor_to": floor,
                "side": {"type": "string", "enum": ["north", "east", "south", "west"]},
                "view_key": view_key,
                "type_key": type_key,
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_FOUND_ROOMS},
            },
        ),
        _command(
            "draft_request",
            {
                "kind": {"type": "string", "enum": ["group", "booking"]},
                "guests": {"type": "integer", "minimum": 1, "maximum": MAX_GROUP_GUESTS},
                "check_in": {"type": "string", "format": "date"},
                "check_out": {"type": "string", "format": "date"},
                "room_numbers": {"type": "array", "items": {"type": "string"}},
                "view_key": view_key,
                "type_key": type_key,
                "wishes": {"type": "string", "maxLength": MAX_WISHES_LENGTH},
            },
            required=["kind"],
        ),
    ]
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "maxItems": MAX_COMMANDS,
                    "items": {"anyOf": commands},
                },
                "reply": {
                    "type": "string",
                    "maxLength": MAX_REPLY_LENGTH,
                    "description": _REPLY_DESCRIPTION,
                },
            },
            "required": ["commands", "reply"],
        },
    )


def _key_schema(keys: Sequence[str]) -> dict[str, Any]:
    # Пустой enum JSON Schema запрещает: у сцены без видов поле просто строка,
    # а ключ не из сцены отбросит проверка команды.
    return {"type": "string", "enum": list(keys)} if keys else {"type": "string"}


def _command(
    kind: str, properties: dict[str, Any], *, required: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"type": {"const": kind}, **properties},
        "required": ["type", *required],
    }
