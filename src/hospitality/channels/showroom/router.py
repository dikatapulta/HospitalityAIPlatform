"""HTTP-маршруты 3D-витрины для посетителей (spec 0069 §4, ADR-025).

Анонимная входная точка (ADR-025 условие 8, как веб-чат — ADR-008 §6): контекст
тенанта маршрут ставит сам по slug (`access.open_showroom`), в общий
`TenantResolver` slug не входит. Отказы: витрина не видна снаружи — 404
`ERR-SHOWROOM-001` раньше всего; нет доступа — 403 `005`; лимит ввода кода — 429 `003`.

PR (б) и (в) подключают свои маршруты строкой `include_router` в конце файла —
навигацию (`navigate.py`) и запросы (`request_form.py`); остальное не трогают.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hospitality.channels.showroom import access, assets, page, request_form
from hospitality.channels.showroom.texts import pick_language
from hospitality.modules.showroom import api as showroom_api
from hospitality.shared.clientip import client_ip
from hospitality.shared.errors import ErrorResponse
from hospitality.shared.tenancy import tenant_context

router = APIRouter(prefix="/showroom", tags=["showroom"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


@router.get("/static/{name}", include_in_schema=False)
async def static_asset(name: str, v: str | None = None) -> Response:
    """Статика витрины из памяти; верная версия в адресе — кэш навсегда."""
    asset = assets.ASSETS.get(name)
    if asset is None:
        return Response(status_code=404)
    cache = (
        "public, max-age=31536000, immutable"
        if v == asset.version or name == assets.THREE_FILE
        else "no-cache"
    )
    return Response(
        content=asset.content,
        media_type=asset.media_type,
        headers={"Cache-Control": cache, "ETag": asset.etag, "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{tenant_slug}", response_class=HTMLResponse, responses=_ERRORS)
async def showroom_page(
    request: Request, tenant_slug: str, lang: str | None = None
) -> HTMLResponse:
    """Страница посетителя; без доступа — экран «Введите код доступа» (§2.2)."""
    showroom = await access.open_showroom(tenant_slug)
    language = pick_language(lang, request.headers.get("accept-language"))
    if access.has_access(showroom, request):
        html = page.render_page(
            slug=tenant_slug,
            hotel_name=showroom.hotel_name,
            language=language,
            blocks=await request_form.page_block(showroom),
        )
    else:
        html = page.render_code_screen(
            slug=tenant_slug, hotel_name=showroom.hotel_name, language=language
        )
    return HTMLResponse(html, headers=page.HEADERS)


class AccessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=showroom_api.MAX_ACCESS_CODE_LENGTH)


@router.post("/{tenant_slug}/access", status_code=204, responses=_ERRORS)
async def enter_code(request: Request, tenant_slug: str, body: AccessBody) -> Response:
    """Проверить код и поставить cookie доступа на 7 дней (§2.2)."""
    showroom = await access.open_showroom(tenant_slug)
    if showroom.settings.public_enabled:
        return Response(status_code=204)
    await access.check_code(showroom, body.code, client_ip=client_ip(request))
    response = Response(status_code=204)
    access.set_access_cookie(response, showroom)
    return response


@router.get("/{tenant_slug}/scene", responses=_ERRORS)
async def scene(request: Request, tenant_slug: str) -> JSONResponse:
    """Сцена для просмотрщика: здание числами, без имён и занятости (ADR-025 условие 1)."""
    showroom = await access.open_showroom(tenant_slug)
    access.require_access(showroom, request)
    with tenant_context(showroom.tenant_id):
        current = await showroom_api.get_scene()
    if current is None:
        raise access.denied()
    return JSONResponse(
        {
            "hotel": showroom.hotel_name,
            "scene_version": current.scene_version,
            "scene": current.scene.model_dump(mode="json"),
        },
        headers={"Cache-Control": "no-store"},
    )


class RoomsView(BaseModel):
    rooms: list[showroom_api.RoomMatch]


@router.get("/{tenant_slug}/rooms", responses=_ERRORS)
async def rooms(
    request: Request, tenant_slug: str, query: Annotated[showroom_api.RoomQuery, Query()]
) -> RoomsView:
    """Подбор номеров панели — тот же `find_rooms`, что у помощника (P-5)."""
    showroom = await access.open_showroom(tenant_slug)
    access.require_access(showroom, request)
    with tenant_context(showroom.tenant_id):
        found = await showroom_api.find_rooms(query)
    return RoomsView(rooms=found)


# PR (в) #805: запросы групп и гостей (`POST …/requests`).
router.include_router(request_form.router)
