"""Страницы раздела «Наставник» (`readme/mentor.md`): вкладки «Наставника» и
«Предложить идею» — каждая одним маршрутом, контекст собирают `mentor.py` и `ideas.py`.

Канон авторизации и рендера — `pages.py`; действия — `mentor_api.py` и `ideas_api.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import ideas, mentor, pages

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/mentor",
    response_class=HTMLResponse,
    summary="«Наставник»: вопросы по кодексу, «Тренировка», кодекс (spec 0048, #492)",
)
@router.get("/{tenant_slug}/mentor/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def mentor_page(request: Request, tenant_slug: str, tab: str = "ask") -> Response:
    """Вкладка «Наставника» (`mentor.TABS`): чат «Спросить» и «Тренировка», «Кодекс»,
    «Где ошибаются» (старшему и менеджеру). Раздел выключен у отеля или вкладки
    нет — 404. Поток ответа и действия — `mentor_api.py`."""
    del tenant_slug
    result = await pages.page_context(request, mentor.page_role(tab))
    if isinstance(result, Response):
        return result
    template, context = await mentor.build_page(result, tab)
    return await pages.hotel_page(result, template, **context)


@router.get(
    "/{tenant_slug}/ideas",
    response_class=HTMLResponse,
    summary="«Предложить идею»: идея разработчику, интервью служб, «Все идеи» (spec 0071, #871)",
)
@router.get("/{tenant_slug}/ideas/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def ideas_page(
    request: Request,
    tenant_slug: str,
    tab: str = "new",
    show: Annotated[str | None, Query()] = None,
) -> Response:
    """Вкладка «Предложить идею» (`ideas.TABS`): «Новая идея» и «Мои идеи» — любому уровню,
    «Интервью» — старшему и менеджеру, «Все идеи» — суперадмину (`?show=all` — и
    разобранные). Раздел выключен у отеля или вкладки нет — 404. Действия — `ideas_api.py`."""
    del tenant_slug
    result = await pages.page_context(request, ideas.page_role(tab))
    if isinstance(result, Response):
        return result
    template, context = await ideas.build_page(result, tab, show_all=show == "all")
    return await pages.hotel_page(result, template, **context)
