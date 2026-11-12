"""Страницы раздела «Служебные записки» (`readme/memos.md`): вкладки с поиском, карточка
записи, страница печати, форма черновика; объяснительные — требование, его фото, черновик
и «Дополнить» (PR 5б). Контекст собирают `memos.py` («Моя подпись»), `memos_card.py`
(с `memos_search.py`), `memos_print.py`, `memos_explanations.py` и `memos_demands.py`.

Канон авторизации и рендера — `pages.py`; действия — `memos_actions_api.py` и
`memo_signature_api.py`. Маршруты записи (`new`, `{id}`) объявлены раньше вкладки:
иначе `…/memos/new` попал бы во вкладку `new` и ответил 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.modules.memos import api as memos_api
from hospitality.platform.files import file_response
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import (
    memos,
    memos_card,
    memos_demands,
    memos_explanations,
    memos_print,
    memos_search,
    pages,
)
from hospitality.staff_portal.memos_people import can_demand

router = APIRouter(tags=["staff-portal"])
logger = get_logger(module=__name__)


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
    "/{tenant_slug}/memos/explanations/new", response_class=HTMLResponse, include_in_schema=False
)
async def explanation_new(
    request: Request, tenant_slug: str, demand: uuid.UUID | None = None
) -> Response:
    """Новая объяснительная — любому уровню: по требованию (`?demand=`, только сотруднику
    после «Получил(а)») или адресату по своей инициативе (§6)."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    await memos.require_section(result)
    context = {
        **memos.tabs_context(result, None),
        **await memos_explanations.new_context(result, demand),
    }
    return await pages.hotel_page(result, "memo_explanation.html", **context)


@router.get(
    "/{tenant_slug}/memos/demands/new", response_class=HTMLResponse, include_in_schema=False
)
async def demand_new(request: Request, tenant_slug: str) -> Response:
    """«Затребовать объяснение» — `senior` и `manager` (§6); остальным — 404."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    await memos.require_section(result)
    if not can_demand(result):
        raise HTTPException(status_code=404)
    context = await memos_demands.new_context(result)
    return await pages.hotel_page(result, "memo_demand_new.html", **context)


@router.get(
    "/{tenant_slug}/memos/demands/{demand_id:uuid}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def demand_page(request: Request, tenant_slug: str, demand_id: uuid.UUID) -> Response:
    """Требование — затребовавшему и сотруднику; чужое — ERR-MEMO-001 (404)."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    config = await memos.require_section(result)
    context = await memos_demands.page_context(result, demand_id, config.tzinfo)
    return await pages.hotel_page(result, "memo_demand.html", **context)


@router.get(
    "/{tenant_slug}/memos/demands/{demand_id:uuid}/photo",
    include_in_schema=False,
)
async def demand_photo(request: Request, tenant_slug: str, demand_id: uuid.UUID) -> Response:
    """Фото требования — тем же двоим (модуль); выдача — `file_response` узла:
    картинка, `no-store`, `nosniff`, `CSP: sandbox`. Открытие — строка лога с id."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    await memos.require_section(result)
    stored = await memos_api.read_demand_photo(result, demand_id)
    logger.info(
        "staff.memo_demand_photo_read", demand_id=str(demand_id), user_id=str(result.user_id)
    )
    return file_response(stored)


@router.get(
    "/{tenant_slug}/memos/{memo_id:uuid}/edit", response_class=HTMLResponse, include_in_schema=False
)
async def memo_edit(request: Request, tenant_slug: str, memo_id: uuid.UUID) -> Response:
    """Правка своего черновика — записки или объяснительной; чужой или отправленный — 404."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    config = await memos.require_section(result)
    card = await memos_card.own_draft(result, memo_id)
    if card.kind is memos_api.MemoKind.EXPLANATION:
        context = {
            **memos.tabs_context(result, None),
            **await memos_explanations.edit_context(result, card),
        }
        return await pages.hotel_page(result, "memo_explanation.html", **context)
    draft = await memos_card.draft_context(result, config, card)
    context = {**memos.tabs_context(result, None), **draft}
    return await pages.hotel_page(result, "memo_draft.html", **context)


@router.get(
    "/{tenant_slug}/memos/{memo_id:uuid}/supplement",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def explanation_supplement(
    request: Request, tenant_slug: str, memo_id: uuid.UUID
) -> Response:
    """«Дополнить» свою поданную объяснительную (§6.4); иное — 404."""
    del tenant_slug
    result = await pages.page_context(request, memos.any_role)
    if isinstance(result, Response):
        return result
    await memos.require_section(result)
    context = {
        **memos.tabs_context(result, None),
        **await memos_explanations.supplement_context(result, memo_id),
    }
    return await pages.hotel_page(result, "memo_explanation.html", **context)


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
