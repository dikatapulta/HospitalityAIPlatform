"""Страницы раздела «Товары и меню» (`readme/room-service.md`): панель каталогов
room service и её фрагмент после каждого действия.

Канон авторизации и рендера — `pages.py`; действия — `catalog_api.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import browser, catalog, pages
from hospitality.staff_portal.rendering import render_page

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/catalog",
    response_class=HTMLResponse,
    summary="Товары и меню: каталоги room service (spec 0038 §6 PR 4, #418)",
)
async def catalog_page(
    request: Request,
    tenant_slug: str,
    tab: Annotated[str | None, Query(alias="catalog")] = None,
) -> Response:
    """Вкладка каталога своей службы (менеджеру — обе): разделы, товары,
    «принимаем». Room service выключен — 404; своих каталогов нет или вкладка
    чужая — «нет доступа» (`catalog.page_target`). Действия — `catalog_api.py`,
    обновление после действия — фрагмент, `static/catalog.js`."""
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    target = await catalog.page_target(result, tab)
    if target is None:
        return pages.forbidden_page(tenant_slug)
    context = await catalog.build_catalog_context(result, *target)
    page = await pages.hotel_page(result, "catalog.html", **context)
    page.headers["Content-Security-Policy"] = browser.PHOTO_PREVIEW_PAGE_CSP
    return page


@router.get(
    "/{tenant_slug}/catalog/fragment",
    response_class=HTMLResponse,
    summary="Фрагмент панели «Товары и меню» — после каждого действия",
)
async def catalog_fragment(
    request: Request,
    tenant_slug: str,
    tab: Annotated[str | None, Query(alias="catalog")] = None,
) -> Response:
    """Вкладки и каталог без каркаса — JS заменяет ими контейнер; исходы — как у страницы."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    target = await catalog.page_target(result, tab)
    if target is None:
        return Response(status_code=403)
    context = await catalog.build_catalog_context(result, *target)
    return browser.html_page(render_page("_catalog_board.html", **context))
