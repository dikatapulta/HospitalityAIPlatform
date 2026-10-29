"""HTTP-маршруты «Заказать в номер» гостя (spec 0038 §4, PR 5 серии).

Всё — под `/g/{tenant_slug}/{room_number}`, как чат: контекст тенанта ставит
канал по slug, сессия — та же cookie `guest_session` (`router.require_guest_session`).
Комната и проживание — только из сессии, `{room}` пути не используется (канон
чата). Room service у отеля выключен — любой маршрут отвечает 404
`ERR-ROOMSERVICE-002` раньше проверки сессии: страницы нет, а не «войдите».

Страница — статический HTML (`page.py`), данные она берёт JSON'ом `…/menu/data`.
Тела — только JSON: SameSite=Strict cookie отрезает чужие сайты, как у `…/messages`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Cookie, Depends, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.channels.web import service
from hospitality.channels.web.menu import page
from hospitality.channels.web.menu.schemas import MenuCatalog, MenuData, OrderBody
from hospitality.channels.web.router import SESSION_COOKIE, require_guest_session
from hospitality.modules.guests import api as guests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import RoomServiceConfig
from hospitality.shared.errors import AppError, ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

router = APIRouter(prefix="/g", tags=["web-chat"])

GuestAuth = Annotated[
    tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
]
_DISABLED: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Room service выключен (002)"}
}


@router.get(
    "/{tenant_slug}/{room_number}/menu",
    response_class=HTMLResponse,
    summary="Страница «Заказать в номер» (spec 0038)",
    responses=_DISABLED,
)
async def menu_page(
    tenant_slug: str,
    room_number: str,
    guest_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Response:
    """Без сессии — 303 на чат, где гость входит по коду; комната — из сессии."""
    tenant_id = await service.resolve_tenant(tenant_slug)
    with tenant_context(tenant_id):
        await _room_service(tenant_id)
        session = await service.resolve_session(guest_session)
        if session is None:
            return RedirectResponse(
                f"/g/{tenant_slug}/{url_quote(room_number, safe='')}", status_code=303
            )
        logger.info("room_service_menu_opened")
    return HTMLResponse(page.render())


@router.get(
    "/{tenant_slug}/{room_number}/menu/data",
    summary="Каталоги, приём заказов и открытые заказы проживания",
    responses=_DISABLED,
)
async def menu_data(auth: GuestAuth) -> MenuData:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        config = await _room_service(tenant_id)
        catalogs: list[MenuCatalog] = []
        for catalog in room_service_api.RoomServiceCatalog:
            if catalog.value not in config.catalogs:
                continue
            settings = await room_service_api.get_settings(catalog)
            menu = await room_service_api.get_catalog(catalog)
            catalogs.append(
                MenuCatalog(
                    catalog=catalog,
                    accepting_orders=settings.accepting_orders,
                    service_charge_percent=settings.service_charge_percent,
                    delivery_note=settings.delivery_note,
                    sections=menu.sections,
                )
            )
        orders = await room_service_api.list_active_orders_for_stay(session.stay_id)
    return MenuData(
        room_number=session.room_number,
        languages=list(config.languages),
        catalogs=catalogs,
        orders=orders,
    )


@router.post(
    "/{tenant_slug}/{room_number}/menu/quote",
    summary="Итог корзины по ценам сервера (Р-5)",
    responses=_DISABLED,
)
async def menu_quote(
    body: room_service_api.QuoteRequest, auth: GuestAuth
) -> room_service_api.Quote:
    tenant_id, _ = auth
    with tenant_context(tenant_id):
        await _room_service(tenant_id)
        return await room_service_api.quote(body)


@router.post(
    "/{tenant_slug}/{room_number}/orders",
    status_code=status.HTTP_201_CREATED,
    summary="Оформить заказ; повтор ключа — 200 с тем же заказом",
    responses={
        **_DISABLED,
        409: {"model": ErrorResponse, "description": "Не принимает, закончилось, итог (003–005)"},
        429: {"model": ErrorResponse, "description": "Лимит заказов проживания (006)"},
    },
)
async def place_order(
    body: OrderBody, auth: GuestAuth, response: Response
) -> room_service_api.OrderRead:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _room_service(tenant_id)
        placed = await room_service_api.place_order(
            room_service_api.OrderPlace(
                catalog=body.catalog,
                stay_id=session.stay_id,
                guest_identity_id=session.guest_identity_id,
                client_order_key=body.client_order_key,
                guest_language=body.language,
                lines=body.lines,
                comment=body.comment,
                expected_total_minor=body.expected_total_minor,
            )
        )
    if placed.duplicate:
        response.status_code = status.HTTP_200_OK
    return placed.order


@router.get(
    "/{tenant_slug}/{room_number}/orders/{order_id}",
    summary="Заказ проживания — стадия, состав, суммы",
    responses={404: {"model": ErrorResponse, "description": "Выключен (002) или чужой (008)"}},
)
async def get_order(order_id: uuid.UUID, auth: GuestAuth) -> room_service_api.OrderRead:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _room_service(tenant_id)
        return await room_service_api.get_order_for_stay(order_id, session.stay_id)


@router.post(
    "/{tenant_slug}/{room_number}/orders/{order_id}/cancel",
    summary="Гость отменяет заказ, пока его не оплатили (не приняли)",
    responses={
        404: {"model": ErrorResponse, "description": "Выключен (002) или чужой (008)"},
        409: {"model": ErrorResponse, "description": "Заказ уже принят (007)"},
    },
)
async def cancel_order(order_id: uuid.UUID, auth: GuestAuth) -> room_service_api.OrderRead:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _room_service(tenant_id)
        return await room_service_api.cancel_order_by_guest(order_id, session.stay_id)


async def _room_service(tenant_id: uuid.UUID) -> RoomServiceConfig:
    """Room service отеля; выключен или не настроен — 404 ERR-ROOMSERVICE-002 (Р-10)."""
    config = await service.room_service_config()
    if config is None:
        logger.info("room_service_disabled_request", tenant_id=str(tenant_id))
        raise AppError(
            code=room_service_api.ERR_ROOMSERVICE_CATALOG_DISABLED,
            message="Room service is disabled",
            status_code=404,
        )
    return config
