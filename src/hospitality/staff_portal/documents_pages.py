"""Страницы раздела «Документы для АВР» (`readme/documents.md`): таблица реквизитов,
«Скачать для 1С» и выдача файла запроса.

Канон авторизации и рендера — `pages.py`; действия — `act_request_api.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.modules.documents import api as documents_api
from hospitality.platform.files import file_response
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import act_requests, pages

logger = get_logger(module=__name__)

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/act-requests",
    response_class=HTMLResponse,
    summary="Документы для АВР: реквизиты от гостей (spec 0049, #493)",
)
async def act_requests_page(request: Request, tenant_slug: str) -> Response:
    """Таблица реквизитов за период: правка в строке, «Проверено», файлы, шаги
    заявки. Видят служба «Бухгалтерия», ресепшен и менеджер
    (`act_requests.can_handle_act_requests`); выключатель Р-7 прячет только пункт
    меню — прошлые запросы открываются до их срока. Без поллинга: после действия
    `static/act_requests.js` перезагружает страницу и не затирает правку."""
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not act_requests.check_right(result):
        return pages.forbidden_page(tenant_slug)
    context = await act_requests.build_act_requests_context(result, dict(request.query_params))
    return await pages.hotel_page(result, "act_requests.html", **context)


@router.get(
    "/{tenant_slug}/act-requests/export",
    summary="Скачать проверенные реквизиты для 1С (spec 0052, #517)",
    include_in_schema=False,
)
async def act_requests_export(request: Request, tenant_slug: str) -> Response:
    """Excel для загрузки контрагентов в 1С: строки «Проверено» за даты фильтра.
    Право — как у страницы; строк нет или их больше потолка — назад на страницу
    с пояснением (`act_requests.export_response`)."""
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not act_requests.check_right(result):
        return pages.forbidden_page(tenant_slug)
    return await act_requests.export_response(result, dict(request.query_params))


@router.get(
    "/{tenant_slug}/act-requests/{act_request_id}/files/{file_id}",
    summary="Файл запроса реквизитов (вложением, без кэша)",
    include_in_schema=False,
)
async def act_request_file(
    request: Request, tenant_slug: str, act_request_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    """Файл этого запроса и только его (ERR-DOCUMENTS-004 — иначе); выдача —
    `file_response` узла: PDF и DOCX вложением, `no-store`, `CSP: sandbox`."""
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    if not act_requests.check_right(result):
        return pages.forbidden_page(tenant_slug)
    stored = await documents_api.read_act_request_file(act_request_id, file_id)
    logger.info(
        "staff.act_request_file_read",
        act_request_id=str(act_request_id),
        file_id=str(file_id),
        user_id=str(result.user_id),
    )
    return file_response(stored)
