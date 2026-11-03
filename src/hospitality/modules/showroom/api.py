"""Публичный интерфейс модуля showroom (spec 0069 §4, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: страница посетителя (`channels/showroom`),
кабинет (`staff_portal`), команда кода доступа и демо-сид (`tools/`) импортируют ТОЛЬКО
отсюда — остальные файлы модуля приватны (контракт 5 import-linter). Здесь нет логики,
только контракт.
"""

from __future__ import annotations

from hospitality.modules.showroom.scene import (
    ERR_SHOWROOM_SCENE_INVALID,
    MAX_SCENE_BYTES,
    MAX_SCENE_ROOMS,
    SIDE_KEYS,
    Building,
    Floor,
    Room,
    RoomTypeInfo,
    SceneDocument,
    SideInfo,
    SideKey,
    Space,
    ViewInfo,
    parse_scene,
)
from hospitality.modules.showroom.schemas import (
    MAX_ROOMS_PER_QUERY,
    RoomMatch,
    RoomQuery,
    SceneRead,
)
from hospitality.modules.showroom.service import (
    ERR_SHOWROOM_ACCESS_DENIED,
    ERR_SHOWROOM_DISABLED,
    ERR_SHOWROOM_RATE_LIMITED,
    MAX_ACCESS_CODE_LENGTH,
    MIN_ACCESS_CODE_LENGTH,
    access_token,
    check_access_code_format,
    find_rooms,
    get_scene,
    has_scene,
    load_settings,
    match_rooms,
    replace_scene,
    room_views,
    set_access_code,
    token_matches,
    verify_access_code,
)

__all__ = [
    "ERR_SHOWROOM_ACCESS_DENIED",
    "ERR_SHOWROOM_DISABLED",
    "ERR_SHOWROOM_RATE_LIMITED",
    "ERR_SHOWROOM_SCENE_INVALID",
    "MAX_ACCESS_CODE_LENGTH",
    "MAX_ROOMS_PER_QUERY",
    "MAX_SCENE_BYTES",
    "MAX_SCENE_ROOMS",
    "MIN_ACCESS_CODE_LENGTH",
    "SIDE_KEYS",
    "Building",
    "Floor",
    "Room",
    "RoomMatch",
    "RoomQuery",
    "RoomTypeInfo",
    "SceneDocument",
    "SceneRead",
    "SideInfo",
    "SideKey",
    "Space",
    "ViewInfo",
    "access_token",
    "check_access_code_format",
    "find_rooms",
    "get_scene",
    "has_scene",
    "load_settings",
    "match_rooms",
    "parse_scene",
    "replace_scene",
    "room_views",
    "set_access_code",
    "token_matches",
    "verify_access_code",
]
