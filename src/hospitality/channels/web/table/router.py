"""HTTP-маршруты «Забронировать столик» гостя (spec 0064 §4, PR 2 серии) — канон `menu/router.py`.

Всё — под `/g/{tenant_slug}/{room_number}`, как чат: контекст тенанта ставит канал
по slug, сессия — та же cookie `guest_session` (`router.require_guest_session`).
Комната и проживание — только из сессии, `{room}` пути не используется. Бронь у
отеля выключена или ресторанов нет — любой маршрут отвечает 404
`ERR-TABLEBOOKING-001` раньше проверки сессии: страницы нет, а не «войдите».

Страница — статический HTML (`page.py`), данные она берёт JSON'ом `…/table/data`.
Тела — только JSON: SameSite=Strict cookie отрезает чужие сайты, как у `…/orders`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Cookie, Depends, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.channels.web import service
from hospitality.channels.web.router import SESSION_COOKIE, require_guest_session
from hospitality.channels.web.table import page
from hospitality.channels.web.table.hotel import table_booking_config
from hospitality.channels.web.table.schemas import BookingBody, TableData
from hospitality.modules.guests import api as guests_api
from hospitality.modules.table_booking import api as table_booking_api
from hospitality.shared.errors import AppError, ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

router = APIRouter(prefix="/g", tags=["web-chat"])

GuestAuth = Annotated[
    tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
]
_DISABLED: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Бронь выключена или ресторана нет (001)"}
}


@router.get(
    "/{tenant_slug}/{room_number}/table",
    response_class=HTMLResponse,
    summary="Страница «Забронировать столик» (spec 0064)",
    responses=_DISABLED,
)
async def table_page(
    tenant_slug: str,
    room_number: str,
    guest_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Response:
    """Без сессии — 303 на чат, где гость входит по коду; комната — из сессии."""
    tenant_id = await service.resolve_tenant(tenant_slug)
    with tenant_context(tenant_id):
        await _table_booking(tenant_id)
        session = await service.resolve_session(guest_session)
        if session is None:
            return RedirectResponse(
                f"/g/{tenant_slug}/{url_quote(room_number, safe='')}", status_code=303
            )
        logger.info("table_booking_page_opened")
    return HTMLResponse(page.render())


@router.get(
    "/{tenant_slug}/{room_number}/table/data",
    summary="Рестораны, дни и слоты, брони проживания",
    responses=_DISABLED,
)
async def table_data(auth: GuestAuth) -> TableData:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _table_booking(tenant_id)
        options = await table_booking_api.get_options(session.stay_id)
        bookings = await table_booking_api.list_bookings_for_stay(session.stay_id)
    return TableData(
        room_number=session.room_number,
        today=options.today,
        restaurants=options.restaurants,
        bookings=bookings,
    )


@router.post(
    "/{tenant_slug}/{room_number}/table/bookings",
    status_code=status.HTTP_201_CREATED,
    summary="Отправить запрос на столик; повтор ключа — 200 с той же бронью",
    responses={
        **_DISABLED,
        422: {"model": ErrorResponse, "description": "Дата, время или гости вне правил (002)"},
        429: {"model": ErrorResponse, "description": "Лимит запросов проживания (003)"},
    },
)
async def request_booking(
    body: BookingBody, auth: GuestAuth, response: Response
) -> table_booking_api.BookingRead:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _table_booking(tenant_id)
        placed = await table_booking_api.request_booking(
            session.stay_id,
            session.guest_identity_id,
            table_booking_api.BookingCreate(
                client_key=body.client_key,
                restaurant_key=body.restaurant_key,
                date=body.date,
                time=body.time,
                party_size=body.party_size,
                guest_name=body.guest_name,
                wishes=body.wishes,
                guest_language=body.language,
            ),
        )
    if placed.duplicate:
        response.status_code = status.HTTP_200_OK
    return placed.booking


@router.get(
    "/{tenant_slug}/{room_number}/table/bookings/{booking_id}",
    summary="Бронь проживания — стадия, ресторан, час",
    responses={404: {"model": ErrorResponse, "description": "Выключена (001) или чужая (004)"}},
)
async def get_booking(booking_id: uuid.UUID, auth: GuestAuth) -> table_booking_api.BookingRead:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _table_booking(tenant_id)
        return await table_booking_api.get_booking_for_stay(booking_id, session.stay_id)


@router.post(
    "/{tenant_slug}/{room_number}/table/bookings/{booking_id}/cancel",
    summary="Гость отменяет запрос до ответа или бронь после «Подтвердить»",
    responses={
        404: {"model": ErrorResponse, "description": "Выключена (001) или чужая (004)"},
        409: {"model": ErrorResponse, "description": "Отмена неприменима (006)"},
    },
)
async def cancel_booking(booking_id: uuid.UUID, auth: GuestAuth) -> table_booking_api.BookingRead:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        await _table_booking(tenant_id)
        return await table_booking_api.cancel_by_guest(booking_id, session.stay_id)


async def _table_booking(tenant_id: uuid.UUID) -> None:
    """Бронь отеля; выключена или ресторанов нет — 404 ERR-TABLEBOOKING-001 (§2.6)."""
    if await table_booking_config() is None:
        logger.info("table_booking_disabled_request", tenant_id=str(tenant_id))
        raise AppError(
            code=table_booking_api.ERR_TABLEBOOKING_DISABLED,
            message="Table booking is disabled",
            status_code=404,
        )
