"""Страницы раздела «Служебные записки» (`readme/memos.md`): вкладки — одним маршрутом,
контекст собирает `memos.py`.

Канон авторизации и рендера — `pages.py`; действия — `memo_signature_api.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import memos, pages

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/memos",
    response_class=HTMLResponse,
    summary="«Служебные записки»: подпись, записки и объяснительные (spec 0077, #936)",
)
@router.get("/{tenant_slug}/memos/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def memos_page(request: Request, tenant_slug: str, tab: str | None = None) -> Response:
    """Вкладка раздела (`memos.TABS`) — любому уровню, только своё. Без вкладки — первая.
    Раздел выключен у отеля или вкладки нет — 404."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    template, context = await memos.build_page(result, tab or next(iter(memos.TABS)))
    return await pages.hotel_page(result, template, **context)
