"""Страницы раздела «Очередь заявок» (`readme/requests.md`): очередь и её фрагмент,
фото гостя к заявке, форма «Новая заявка» и её поля для окна каркаса, «Диалоги».

Канон авторизации и рендера — `pages.py`; действия — `queue_api.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.modules.requests import api as requests_api
from hospitality.platform.files import file_response
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import conversations, new_request, pages, scope
from hospitality.staff_portal import queue as queue_page
from hospitality.staff_portal.browser import html_page
from hospitality.staff_portal.queue import build_queue_context
from hospitality.staff_portal.queue_filters import parse_queue_tab
from hospitality.staff_portal.rendering import render_page

logger = get_logger(module=__name__)

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/requests",
    response_class=HTMLResponse,
    summary="Очередь заявок (spec 0033 §5, закрывает #56)",
)
async def requests_queue(request: Request, tenant_slug: str) -> Response:
    """Лента открытых (`new`+`in_progress`), «закрытые за сегодня» или «за 7
    дней»; фильтры-чипсы — категория и «мои», поиск `q` — комната или `#N`.
    Действия — JSON-эндпоинты `queue_api.py`, обновление — поллинг
    `static/queue.js` каждые 15 с."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    category_key = request.query_params.get("category") or None
    if category_key and scope.is_foreign_service(result, category_key):
        return await _foreign_service_page(result)
    # Плашка «заявка #N создана» — возврат с формы ручного приёма (spec 0035
    # §5); у fragment-эндпоинта её нет: партиал списка плашку не рисует, а
    # поллинг каждые 15 с показывал бы её снова и снова.
    context = await build_queue_context(
        result,
        tab=parse_queue_tab(request.query_params.get("tab")),
        category_key=category_key,
        mine=request.query_params.get("mine") == "1",
        search=request.query_params.get("q"),
        created=request.query_params.get("created"),
        created_category_id=request.query_params.get("service"),
    )
    return await pages.hotel_page(result, "queue.html", **context)


async def _foreign_service_page(staff: StaffContext) -> Response:
    """403 на фильтр `?category=` чужой службы (spec 0045 §5): страница отеля с
    меню и сигналом — сотрудник в своём кабинете, ему закрыта только эта лента."""
    logger.warning("staff.queue_foreign_service_denied", user_id=str(staff.user_id))
    return await pages.hotel_page(
        staff,
        "service_forbidden.html",
        status_code=403,
        service_names=await queue_page.service_names(staff),
        queue_href=f"/staff/{staff.tenant_slug}/requests",
    )


@router.get(
    "/{tenant_slug}/requests/fragment",
    response_class=HTMLResponse,
    summary="Фрагмент списка очереди (поллинг 15 с)",
)
async def requests_queue_fragment(request: Request, tenant_slug: str) -> Response:
    """Тот же список с теми же фильтрами, но без каркаса страницы: JS заменяет
    им контейнер списка. GET данных не меняет (CSRF-контракт); исходы — как у
    страницы (истёкшая сессия → редирект, JS уводит на логин по location)."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    category_key = request.query_params.get("category") or None
    if category_key and scope.is_foreign_service(result, category_key):
        # Поллинг не заменяет список на не-200 ответ (queue.js) — тела не нужно.
        return Response(status_code=403)
    context = await build_queue_context(
        result,
        tab=parse_queue_tab(request.query_params.get("tab")),
        category_key=category_key,
        mine=request.query_params.get("mine") == "1",
        search=request.query_params.get("q"),
    )
    return html_page(render_page("_queue_list.html", **context))


@router.get(
    "/{tenant_slug}/requests/{request_id}/photos/{file_id}",
    summary="Фото гостя к заявке (spec 0081 §7): картинка в границе служб сотрудника",
    include_in_schema=False,
)
async def request_photo(
    request: Request, tenant_slug: str, request_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    """Фото этой заявки и только её; заявка не в службах сотрудника — 404
    `ERR-REQUESTS-010`, как несуществующее. Выдача — `file_response` узла:
    картинка без вложения, `private, max-age=3600`, `nosniff`, `CSP: sandbox`.
    Открытие — строка лога с `user_id`, без содержимого."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    stored = await requests_api.read_request_photo(
        request_id, file_id, allowed_category_ids=await scope.load_allowed_category_ids(result)
    )
    logger.info(
        "staff.request_photo_read",
        request_id=str(request_id),
        file_id=str(file_id),
        user_id=str(result.user_id),
    )
    return file_response(stored)


@router.get(
    "/{tenant_slug}/requests/new",
    response_class=HTMLResponse,
    summary="Форма «Новая заявка» (spec 0035 §5, закрывает #299)",
)
async def new_request_page(request: Request, tenant_slug: str) -> Response:
    """Ручной приём: комната → служба чипсами → фраза (заявку сказали по
    телефону или поймали горничную в коридоре). Роль любая — мини-матрица
    docs/RBAC.md; создание — JSON-действие `POST …/api/requests`, отправляет
    `static/new_request.js`."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    context = await new_request.build_new_request_context(result)
    return await pages.hotel_page(result, "new_request.html", **context)


@router.get(
    "/{tenant_slug}/requests/new/fields",
    response_class=HTMLResponse,
    summary="Поля окна «Новая заявка» (#636)",
)
async def new_request_fields(request: Request, tenant_slug: str) -> Response:
    """Фрагмент полей для окна каркаса: `static/new_request.js` берёт его при
    каждом открытии окна. Права и исходы — как у страницы формы (истёкшая
    сессия → редирект, JS уводит на вход); GET данных не меняет."""
    del tenant_slug
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    form = await new_request.build_form_context(result)
    return html_page(render_page("_new_request_fields.html", form=form, mode="dialog"))


@router.get(
    "/{tenant_slug}/conversations",
    response_class=HTMLResponse,
    summary="Диалоги: переписка гостей с ботом в веб-чате, только чтение (spec 0067, #767)",
)
async def conversations_page(request: Request, tenant_slug: str) -> Response:
    """Список веб-диалогов отеля, фильтр `?room=` и `?date=` (сутки отеля).
    Ресепшен или менеджер (docs/RBAC.md); раздел выключен
    (`ENABLE_STAFF_CONVERSATIONS`) — 404, как у неизвестного адреса."""
    del tenant_slug
    if not conversations.section_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.reception_role)
    if isinstance(result, Response):
        return result
    found = conversations.parse_filter(
        request.query_params.get("room"), request.query_params.get("date")
    )
    context = await conversations.build_list_context(result, found)
    return await pages.hotel_page(result, "conversations.html", **context)


@router.get(
    "/{tenant_slug}/conversations/{conversation_id}",
    response_class=HTMLResponse,
    summary="Лента одного диалога гостя с ботом, без поля ввода (spec 0067 §2.3)",
)
async def conversation_page(request: Request, tenant_slug: str, conversation_id: str) -> Response:
    """Сообщения гостя и бота по времени отеля; открытие — строка лога без текста.
    Чужой отель, не веб-чат или кривой id — 404 (RLS: чужой диалог не найден)."""
    del tenant_slug
    if not conversations.section_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.reception_role)
    if isinstance(result, Response):
        return result
    try:
        parsed_id = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    context = await conversations.build_feed_context(result, parsed_id)
    if context is None:
        raise HTTPException(status_code=404)
    return await pages.hotel_page(result, "conversation.html", **context)
