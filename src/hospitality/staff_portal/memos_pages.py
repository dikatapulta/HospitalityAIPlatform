"""Страницы раздела «Служебные записки» (`readme/memos.md`): вкладки с поиском, карточка
записи, страница печати, форма черновика. Контекст собирают `memos.py` («Моя подпись»),
`memos_card.py` (с `memos_search.py`) и `memos_print.py`.

Канон авторизации и рендера — `pages.py`; действия — `memos_actions_api.py` и
`memo_signature_api.py`. Маршруты записи (`new`, `{id}`) объявлены раньше вкладки:
иначе `…/memos/new` попал бы во вкладку `new` и ответил 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import memos, memos_card, memos_print, memos_search, pages

router = APIRouter(tags=["staff-portal"])


@router.get("/{tenant_slug}/memos/new", response_class=HTMLResponse, include_in_schema=False)
async def memo_new(request: Request, tenant_slug: str) -> Response:
    """Новый черновик — любому уровню (§3.1)."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    config = await memos.require_section(result)
    context = {
        **memos.tabs_context(result, None),
        **await memos_card.draft_context(result, config, None),
    }
    return await pages.hotel_page(result, "memo_draft.html", **context)


@router.get(
    "/{tenant_slug}/memos/{memo_id:uuid}/edit", response_class=HTMLResponse, include_in_schema=False
)
async def memo_edit(request: Request, tenant_slug: str, memo_id: uuid.UUID) -> Response:
    """Правка своего черновика; чужой или отправленный — 404."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    config = await memos.require_section(result)
    draft = await memos_card.draft_context(result, config, memo_id)
    context = {**memos.tabs_context(result, None), **draft}
    return await pages.hotel_page(result, "memo_draft.html", **context)


@router.get(
    "/{tenant_slug}/memos/{memo_id:uuid}/print",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def memo_print(request: Request, tenant_slug: str, memo_id: uuid.UUID) -> Response:
    """Страница печати отправленной записи — участнику; черновик и чужая — 404. Открытие
    пишет строку журнала просмотров (модуль), как карточка."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    config = await memos.require_section(result)
    context = await memos_print.print_context(result, memo_id, config.tzinfo)
    return await pages.hotel_page(result, "memo_print.html", **context)


@router.get(
    "/{tenant_slug}/memos/{memo_id:uuid}", response_class=HTMLResponse, include_in_schema=False
)
async def memo_card(request: Request, tenant_slug: str, memo_id: uuid.UUID) -> Response:
    """Карточка записи — только участнику; чужая — ERR-MEMO-001 (404). Открытие
    отправленной пишет строку журнала просмотров (модуль)."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    config = await memos.require_section(result)
    card = await memos_card.card_context(result, memo_id, config.tzinfo)
    context = {**memos.tabs_context(result, None), **card}
    return await pages.hotel_page(result, "memo_card.html", **context)


@router.get(
    "/{tenant_slug}/memos",
    response_class=HTMLResponse,
    summary="«Служебные записки»: записки, подпись (spec 0077, #936)",
)
@router.get("/{tenant_slug}/memos/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def memos_page(request: Request, tenant_slug: str, tab: str | None = None) -> Response:
    """Вкладка раздела (`memos.TABS`) — любому уровню, только своё. Без вкладки — первая
    («Ждут меня»). Раздел выключен у отеля или вкладки нет — 404. Поиск и фильтры
    реестра — параметры запроса (`memos_search.parse_filter`)."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    tab = tab or next(iter(memos.TABS))
    if tab not in memos.TABS:
        raise HTTPException(status_code=404)
    config = await memos.require_section(result)
    if tab in memos.BOXES:
        condition = memos_search.parse_filter(request.query_params)
        context = await memos_card.list_context(result, tab, config.tzinfo, condition)
        return await pages.hotel_page(result, "memo_list.html", **context)
    return await pages.hotel_page(
        result, "memo_signature.html", **await memos.signature_page(result, config)
    )
