"""JSON-действия панели «Товары и меню» (spec 0038 §4, §6 PR 4; issue #418).

Канон `turnover_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role`, исходы — конверт ошибок R-8. Room service у отеля
выключен — 404 ERR-ROOMSERVICE-002; каталог чужой службы — 403 ERR-AUTH-003
(`catalog.require_edit`). Каталог раздела и товара не меняется никогда, поэтому
право проверяется до вызова домена без гонки. Тела — схемы `room_service.api`
(`SectionWrite`, `ItemWrite`): цену страница шлёт в тиынах, целыми тенге ×100.
Действия зовёт `static/catalog.js`.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from hospitality.modules.room_service import api as room_service_api
from hospitality.platform import staff_auth
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import StaffRole
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_catalog_edit
from hospitality.staff_portal import catalog, queue
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/catalog", tags=["staff-portal"])

# Уровень — любой; право на каталог — служба каталога или менеджер.
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)

Catalog = room_service_api.RoomServiceCatalog
SectionWrite = room_service_api.SectionWrite
ItemWrite = room_service_api.ItemWrite
MoveDirection = room_service_api.MoveDirection


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: MoveDirection


class AvailableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool


class HiddenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hidden: bool


class AcceptingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepting: bool


async def _actor(request: Request) -> tuple[staff_auth.StaffContext, TenantConfig | None]:
    """CSRF и личность — до всего; затем room service отеля: выключен — 404."""
    require_json_same_origin(request)
    staff = await _any_role(request)
    config = await queue.tenant_config(staff)
    if not catalog.room_service_on(config):
        raise AppError(
            code=room_service_api.ERR_ROOMSERVICE_CATALOG_DISABLED,
            message="Room service is disabled for this hotel",
            status_code=404,
        )
    return staff, config


async def _editor(request: Request, target: Catalog) -> staff_auth.StaffContext:
    staff, config = await _actor(request)
    catalog.require_edit(staff, target, config)
    return staff


async def _section_editor(request: Request, section_id: uuid.UUID) -> staff_auth.StaffContext:
    """Каталог раздела читается только после CSRF и личности: до них запрос
    не вправе ни трогать БД, ни узнавать, есть ли такой раздел."""
    staff, config = await _actor(request)
    catalog.require_edit(staff, await room_service_api.catalog_of_section(section_id), config)
    return staff


async def _item_editor(request: Request, item_id: uuid.UUID) -> staff_auth.StaffContext:
    staff, config = await _actor(request)
    catalog.require_edit(staff, await room_service_api.catalog_of_item(item_id), config)
    return staff


def _done(staff: staff_auth.StaffContext, action: str, **fields: str) -> None:
    logger.info("staff.catalog_edited", action=action, user_id=str(staff.user_id), **fields)
    record_staff_catalog_edit(action)


@router.post("/catalogs/{target}/sections", summary="Новый раздел в конец каталога")
async def create_section(
    request: Request, target: Catalog, body: SectionWrite
) -> room_service_api.SectionRead:
    staff = await _editor(request, target)
    section = await room_service_api.create_section(target, body)
    _done(staff, "section_created", section_id=str(section.id))
    return section


@router.post("/catalogs/{target}/accepting", summary="«Заказы в номер: принимаем» каталога")
async def set_accepting(
    request: Request, target: Catalog, body: AcceptingBody
) -> room_service_api.CatalogSettingsRead:
    staff = await _editor(request, target)
    settings = await room_service_api.set_accepting(target, body.accepting)
    _done(staff, "accepting_set", catalog=target.value, accepting=str(body.accepting))
    return settings


@router.post("/sections/{section_id}", summary="Переименовать раздел")
async def rename_section(
    request: Request, section_id: uuid.UUID, body: SectionWrite
) -> room_service_api.SectionRead:
    staff = await _section_editor(request, section_id)
    section = await room_service_api.rename_section(section_id, body)
    _done(staff, "section_renamed", section_id=str(section_id))
    return section


@router.post("/sections/{section_id}/move", summary="Раздел выше или ниже")
async def move_section(
    request: Request, section_id: uuid.UUID, body: MoveBody
) -> room_service_api.SectionRead:
    staff = await _section_editor(request, section_id)
    section = await room_service_api.move_section(section_id, body.direction)
    _done(staff, "section_moved", section_id=str(section_id))
    return section


@router.post("/items", summary="Новый товар в конец раздела")
async def create_item(request: Request, body: ItemWrite) -> room_service_api.ItemRead:
    staff = await _section_editor(request, body.section_id)
    item = await room_service_api.create_item(body)
    _done(staff, "item_created", item_id=str(item.id))
    return item


@router.post("/items/{item_id}", summary="Сохранить форму товара")
async def update_item(
    request: Request, item_id: uuid.UUID, body: ItemWrite
) -> room_service_api.ItemRead:
    """Раздел другого каталога — 001 от домена: право проверено по каталогу товара."""
    staff = await _item_editor(request, item_id)
    item = await room_service_api.update_item(item_id, body)
    _done(staff, "item_updated", item_id=str(item_id))
    return item


@router.post("/items/{item_id}/available", summary="«В наличии» / «Закончилось»")
async def set_available(
    request: Request, item_id: uuid.UUID, body: AvailableBody
) -> room_service_api.ItemRead:
    staff = await _item_editor(request, item_id)
    item = await room_service_api.set_available(item_id, body.available)
    _done(staff, "item_availability_set", item_id=str(item_id))
    return item


@router.post("/items/{item_id}/hidden", summary="«Скрыть из меню» / «Вернуть в меню»")
async def set_hidden(
    request: Request, item_id: uuid.UUID, body: HiddenBody
) -> room_service_api.ItemRead:
    staff = await _item_editor(request, item_id)
    item = await room_service_api.set_hidden(item_id, body.hidden)
    _done(staff, "item_hidden_set", item_id=str(item_id))
    return item


@router.post("/items/{item_id}/move", summary="Товар выше или ниже в разделе")
async def move_item(
    request: Request, item_id: uuid.UUID, body: MoveBody
) -> room_service_api.ItemRead:
    staff = await _item_editor(request, item_id)
    item = await room_service_api.move_item(item_id, body.direction)
    _done(staff, "item_moved", item_id=str(item_id))
    return item
