"""Страницы раздела хаускипинга (`readme/housekeeping.md`): «Выезды и уборка»,
«Этажи» и доска «Номера» с фрагментами для поллинга.

Канон авторизации и рендера — `pages.py`; действия — `turnover_api.py` и `floors_api.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import floors, floors_view, pages, room_board, turnovers
from hospitality.staff_portal.browser import html_page
from hospitality.staff_portal.rendering import render_page

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/turnovers",
    response_class=HTMLResponse,
    summary="Выезды и уборка: путь номера после выезда (spec 0046, #476)",
)
async def turnovers_page(request: Request, tenant_slug: str) -> Response:
    """Форма «Отметить выезд» ресепшену и доска номеров по шагам; видят ресепшен,
    менеджер и хаускипинг (`turnovers.rights_for`), остальным — «нет доступа».
    Раздел выключен (`ENABLE_TURNOVER_BOARD`) — 404, как у неизвестного адреса.
    Действия — `turnover_api.py`, обновление — поллинг `static/turnovers.js`."""
    if not turnovers.board_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not turnovers.rights_for(result).can_view:
        return pages.forbidden_page(tenant_slug)
    context = await turnovers.build_turnovers_context(result, with_form=True)
    return await pages.hotel_page(result, "turnovers.html", **context)


@router.get(
    "/{tenant_slug}/turnovers/fragment",
    response_class=HTMLResponse,
    summary="Фрагмент доски выездов (поллинг 15 с)",
)
async def turnovers_fragment(request: Request, tenant_slug: str) -> Response:
    """Доска без каркаса — JS заменяет ею контейнер; исходы — как у страницы."""
    del tenant_slug
    if not turnovers.board_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not turnovers.rights_for(result).can_view:
        return Response(status_code=403)
    context = await turnovers.build_turnovers_context(result, with_form=False)
    return html_page(render_page("_turnover_board.html", **context))


@router.get(
    "/{tenant_slug}/floors",
    response_class=HTMLResponse,
    summary="Этажи хаускипинга: раздача этажей горничным (spec 0053, #478)",
)
async def floors_page(request: Request, tenant_slug: str, saved: bool = False) -> Response:
    """Доска раздачи этажей старшему уборки и менеджеру, «По этажам» — хаускипингу и
    ресепшену (`floors.rights_for`), остальным — «нет доступа». Раздел выключен
    (`ENABLE_HOUSEKEEPING_FLOORS`) — 404. Сохранение — `floors_api.py`."""
    if not floors.section_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not floors.rights_for(result).can_view:
        return pages.forbidden_page(tenant_slug)
    context = await floors_view.build_floors_context(result, saved=saved)
    return await pages.hotel_page(result, "floors.html", **context)


@router.get(
    "/{tenant_slug}/rooms",
    response_class=HTMLResponse,
    summary="Номера: все номера отеля по этажам (spec 0061, #666)",
)
async def room_board_page(request: Request, tenant_slug: str) -> Response:
    """Доска номеров на живых проживаниях и путях после выезда; видят ресепшен,
    менеджер и хаускипинг (`turnovers.rights_for`), остальным — «нет доступа».
    Выключено (`ENABLE_ROOM_BOARD` или доска выездов) — 404. Действия панели —
    `turnover_api.py`, обновление — поллинг `static/room_board.js`."""
    if not room_board.board_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not turnovers.rights_for(result).can_view:
        return pages.forbidden_page(tenant_slug)
    context = await room_board.build_room_board_context(result, with_steps=True)
    return await pages.hotel_page(result, "room_board.html", **context)


@router.get(
    "/{tenant_slug}/rooms/fragment",
    response_class=HTMLResponse,
    summary="Фрагмент доски «Номера» (поллинг 15 с)",
)
async def room_board_fragment(request: Request, tenant_slug: str) -> Response:
    """Доска без каркаса — JS заменяет ею контейнер; исходы — как у страницы."""
    del tenant_slug
    if not room_board.board_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not turnovers.rights_for(result).can_view:
        return Response(status_code=403)
    context = await room_board.build_room_board_context(result, with_steps=False)
    return html_page(render_page("_room_board.html", **context))
