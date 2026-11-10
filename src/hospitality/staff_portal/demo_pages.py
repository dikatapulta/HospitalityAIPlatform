"""Демо-страницы и «Тестовые данные» (`readme/demo.md`): «Передача смены»,
«Забытые вещи» и кнопка демо-картины смены на staging.

Канон авторизации и рендера — `pages.py`; действие «Тестовых данных» — `demo_data_api.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.staff_portal import (
    browser,
    checkin,
    demo_data,
    demo_handover,
    demo_lost_found,
    pages,
)

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/handover",
    response_class=HTMLResponse,
    summary="Демо «Передача смены» к собранию в отеле (issue #450)",
    include_in_schema=False,
)
async def demo_handover_page(request: Request, tenant_slug: str) -> Response:
    """Касса к сдаче смены одним кликом на выдуманных данных: «загрузка»
    источников и заполнение бланка идут только в браузере
    (`static/demo_handover.js`), данных страница не трогает. `ENABLE_DEMO_HANDOVER`
    — демо целиком; без него `ENABLE_HANDOVER_MANUAL` — только ручной бланк
    (#469); оба выключены — 404, как у неизвестного адреса. Дальше — канон
    `page_context`, роль ресепшен или менеджер (касса — их работа)."""
    settings = get_settings()
    if not (settings.enable_demo_handover or settings.enable_handover_manual):
        raise HTTPException(status_code=404)
    del tenant_slug
    result = await pages.page_context(request, pages.reception_role)
    if isinstance(result, Response):
        return result
    if settings.enable_demo_handover:
        context = demo_handover.build_demo_handover_context(result)
    else:
        context = demo_handover.build_manual_handover_context(result)
    return await pages.hotel_page(result, "demo_handover.html", **context)


@router.get(
    "/{tenant_slug}/lost-found",
    response_class=HTMLResponse,
    summary="Демо «Забытые вещи» к показу руководству отеля (issue #604)",
    include_in_schema=False,
)
async def demo_lost_found_page(
    request: Request,
    tenant_slug: str,
    room: Annotated[str | None, Query()] = None,
) -> Response:
    """Находки на выдуманных данных: поиск, «Найдена вещь» и выдача идут только
    в браузере (`static/demo_lost_found.js`), данных страница не трогает и
    файлов не принимает. `ENABLE_DEMO_LOST_FOUND` выключен — 404, как у
    неизвестного адреса. Дальше — канон `page_context`, любой вошедший:
    находку пишет горничная, ищет ресепшен. `?room=` — номер в форму: она
    открыта сразу. Фото — предпросмотр `blob:`, отсюда CSP
    `PHOTO_PREVIEW_PAGE_CSP`."""
    if not get_settings().enable_demo_lost_found:
        raise HTTPException(status_code=404)
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    today = utc_now().astimezone(await checkin.hotel_zone(result)).date()
    context = demo_lost_found.build_demo_lost_found_context(today, room)
    page = await pages.hotel_page(
        result,
        "demo_lost_found.html",
        tenant_name=result.tenant_name,
        tenant_slug=result.tenant_slug,
        **context,
    )
    page.headers["Content-Security-Policy"] = browser.PHOTO_PREVIEW_PAGE_CSP
    return page


@router.get(
    "/{tenant_slug}/demo-data",
    response_class=HTMLResponse,
    summary="«Тестовые данные»: демо-картина смены кнопкой, только staging (issue #740)",
    include_in_schema=False,
)
async def demo_data_page(request: Request, tenant_slug: str) -> Response:
    """Одна кнопка — та же картина, что `demo_showcase --reset`, без приглашений;
    действие — `demo_data_api.py`. Не контур команды (staging демо-отеля,
    `demo_data.contour_allows`) — 404, как у неизвестного адреса; дальше — канон
    `page_context`, только менеджер."""
    if not demo_data.contour_allows(tenant_slug):
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.manager_role)
    if isinstance(result, Response):
        return result
    return await pages.hotel_page(
        result,
        "demo_data.html",
        tenant_name=result.tenant_name,
        endpoint=f"/staff/{result.tenant_slug}/api/demo-data",
    )
