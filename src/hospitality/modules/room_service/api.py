"""Публичный интерфейс модуля room_service (spec 0038 §4, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: кабинет (панель «Товары и меню»,
PR 4), страница гостя (PR 5) и сид демо-отеля импортируют ТОЛЬКО отсюда —
остальные файлы модуля приватны (контракт 5 import-linter). Здесь нет логики,
только контракт. Заказы (`quote`, `place_order` и др.) — PR 5 серии.
"""

from __future__ import annotations

from hospitality.modules.room_service.models import RoomServiceCatalog
from hospitality.modules.room_service.schemas import (
    MAX_CATALOG_NAME_LENGTH,
    MAX_PRICE_MINOR,
    CatalogRead,
    CatalogSettingsRead,
    ItemRead,
    ItemWrite,
    MoveDirection,
    SectionRead,
    SectionWithItems,
    SectionWrite,
)
from hospitality.modules.room_service.service import (
    ERR_ROOMSERVICE_CATALOG_DISABLED,
    ERR_ROOMSERVICE_NOT_FOUND,
    create_item,
    create_section,
    get_catalog,
    get_settings,
    move_item,
    move_section,
    rename_section,
    set_accepting,
    set_available,
    set_hidden,
    update_item,
)

__all__ = [
    "ERR_ROOMSERVICE_CATALOG_DISABLED",
    "ERR_ROOMSERVICE_NOT_FOUND",
    "MAX_CATALOG_NAME_LENGTH",
    "MAX_PRICE_MINOR",
    "RoomServiceCatalog",
    "CatalogRead",
    "CatalogSettingsRead",
    "ItemRead",
    "ItemWrite",
    "MoveDirection",
    "SectionRead",
    "SectionWithItems",
    "SectionWrite",
    "create_item",
    "create_section",
    "get_catalog",
    "get_settings",
    "move_item",
    "move_section",
    "rename_section",
    "set_accepting",
    "set_available",
    "set_hidden",
    "update_item",
]
