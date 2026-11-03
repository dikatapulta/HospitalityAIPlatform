"""Действия раздела «3D-витрина» кабинета (spec 0069 §4 «Кабинет», PR а).

Канон `mentor_api.py`: личность — `staff_auth.require_role`, раздел выключен у отеля —
404, право на раздел — `showroom.can_view` (чужой службе — 403 `ERR-AUTH-003`).
POST — за CSRF-щитом `browser.require_json_same_origin` (JSON без своего `Origin` —
`ERR-AUTH-009`). Сцена приходит текстом файла в JSON-теле (как кодекс «Наставника»):
так отказ разбора называет путь и причину тем же кодом `ERR-SHOWROOM-002`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hospitality.channels.showroom import access
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import showroom
from hospitality.staff_portal.browser import require_json_same_origin
from hospitality.staff_portal.checkin import format_local
from hospitality.staff_portal.queue import tenant_config

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/showroom", tags=["staff-portal"])

_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)
# Текст файла сцены: предел схемы — 512 КБ байтами; знаков не больше байтов.
_MAX_SCENE_CHARS: Final = showroom_api.MAX_SCENE_BYTES

RoleDependency = Callable[[Request], Awaitable[StaffContext]]


async def _actor(
    request: Request, role: RoleDependency = _any_role, *, csrf: bool = False
) -> tuple[StaffContext, ShowroomConfig]:
    """CSRF (для POST) и личность — до всего; раздел выключен — 404; чужая служба — 403."""
    if csrf:
        require_json_same_origin(request)
    staff = await role(request)
    settings = await showroom.require_section(staff)
    if settings is None:
        raise AppError(
            code=staff_auth.ERR_AUTH_FORBIDDEN,
            message="Showroom is not open to this service",
            status_code=403,
        )
    return staff, settings


@router.get("/scene")
async def scene(request: Request) -> JSONResponse:
    """Сцена для просмотрщика кабинета; не загружена — 404 `ERR-SHOWROOM-001`."""
    await _actor(request)
    current = await showroom_api.get_scene()
    if current is None:
        raise AppError(code=showroom_api.ERR_SHOWROOM_DISABLED, message="No scene", status_code=404)
    return JSONResponse(
        {"scene_version": current.scene_version, "scene": current.scene.model_dump(mode="json")},
        headers={"Cache-Control": "no-store"},
    )


class RoomsView(BaseModel):
    rooms: list[showroom_api.RoomMatch]


@router.get("/rooms")
async def rooms(request: Request, query: Annotated[showroom_api.RoomQuery, Query()]) -> RoomsView:
    """Панель подбора — `find_rooms`, как у страницы посетителя."""
    await _actor(request)
    return RoomsView(rooms=await showroom_api.find_rooms(query))


class SceneUploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=_MAX_SCENE_CHARS)


class SceneUploadedView(BaseModel):
    scene_version: int
    message: str


@router.post("/scene")
async def upload_scene(request: Request, body: SceneUploadBody) -> SceneUploadedView:
    """«Загрузить сцену (JSON)» — менеджер; отказ схемы — 422 `002` с путём, прежняя остаётся."""
    staff, _settings = await _actor(request, _manager_role, csrf=True)
    document = showroom_api.parse_scene(body.text)
    saved = await showroom_api.replace_scene(document, acting_user=staff.actor())
    config = await tenant_config(staff)
    date = format_local(saved.replaced_at, config.tzinfo) if config else ""
    logger.info("staff.showroom_scene_uploaded", user_id=str(staff.user_id))
    return SceneUploadedView(
        scene_version=saved.scene_version,
        message=showroom.UPDATED_TEXT.format(version=saved.scene_version, date=date),
    )


@router.post("/access-cookie", status_code=204)
async def access_cookie(request: Request) -> Response:
    """«Открыть страницу посетителя ↗» — cookie доступа менеджеру без кода (§2.2).

    Страница посетителя не видна снаружи (кабинетный режим) — 404 `001`: cookie ей
    не нужна, а ставить нечем — подпись строится от кода.
    """
    staff, settings = await _actor(request, _manager_role, csrf=True)
    if not showroom.visitor_page_open(settings) or not await showroom_api.has_scene():
        raise AppError(
            code=showroom_api.ERR_SHOWROOM_DISABLED,
            message="Visitor page is not open",
            status_code=404,
        )
    response = Response(status_code=204)
    access.set_access_cookie(
        response,
        access.Showroom(
            tenant_id=staff.tenant_id,
            slug=staff.tenant_slug,
            hotel_name=staff.tenant_name,
            settings=settings,
        ),
    )
    logger.info("staff.showroom_access_cookie", user_id=str(staff.user_id))
    return response
