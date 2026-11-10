"""Страница раздела «3D-витрина» (`readme/showroom.md`).

Канон авторизации и рендера — `pages.py`; сцена, подбор и действия — `showroom_api.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import pages, showroom

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/showroom",
    response_class=HTMLResponse,
    summary="3D-витрина: просмотрщик, подбор номеров, загрузка сцены (spec 0069 §2.2, #803)",
)
async def showroom_page(request: Request, tenant_slug: str) -> Response:
    """Раздел витрины: выключен у отеля — 404; чужой службе — «нет доступа».
    Сцена, подбор и действия менеджера — `showroom_api.py`."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    settings = await showroom.require_section(result)
    if settings is None:
        return pages.forbidden_page(result.tenant_slug)
    context = await showroom.page_context(result, settings)
    return await pages.hotel_page(result, "showroom.html", **context)
