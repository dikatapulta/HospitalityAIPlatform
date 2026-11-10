"""Страница раздела «Заселение» (`readme/checkin.md`): поиск комнаты и форма
«Заселить» (spec 0033 §6, PR E).

Канон авторизации и рендера — `pages.py`; действия карточки Stay — `stay_api.py`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.modules.guests import api as guests_api
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_checkin
from hospitality.staff_portal import browser, checkin, pages

logger = get_logger(module=__name__)

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/checkin",
    response_class=HTMLResponse,
    summary="Страница заселения (spec 0033 §6)",
)
async def checkin_page(request: Request, tenant_slug: str) -> Response:
    """Форма «комната → ночи → гости → Заселить»; `?room=` открывает карточку
    занятой комнаты (или ту же форму с подсказкой «свободна»); `?day=` — день
    блока «Заселены за день» (#832)."""
    del tenant_slug
    result = await pages.page_context(request, pages.reception_role)
    if isinstance(result, Response):
        return result
    context = await checkin.build_checkin_context(
        result, request.query_params.get("room"), day_query=request.query_params.get("day")
    )
    return await pages.hotel_page(result, "checkin.html", **context)


@router.post(
    "/{tenant_slug}/checkin",
    response_class=HTMLResponse,
    summary="Заселить: Guest + Stay + код + QR ссылки привязки",
)
async def checkin_submit(
    request: Request,
    tenant_slug: str,
    room: Annotated[str, Form()] = "",
    nights: Annotated[str, Form()] = "1",
    nights_custom: Annotated[str, Form()] = "",
    guests: Annotated[str, Form()] = "1",
    guests_custom: Annotated[str, Form()] = "",
) -> Response:
    """HTML-форма (канон CSRF — как логин: Origin-проверка + SameSite). Успех
    рендерится СРАЗУ, без redirect: код заселения показывается ровно один раз.
    Повторный submit той же комнаты (refresh) — не дубль, а карточка занятой
    комнаты (ERR-GUESTS-002 держит БД). Дефолты полей — как у логина: урезанный
    POST получает HTML-форму, а не 422-конверт."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    del tenant_slug
    result = await pages.page_context(request, pages.reception_role)
    if isinstance(result, Response):
        return result
    try:
        form = checkin.parse_checkin_form(room, nights, nights_custom, guests, guests_custom)
    except checkin.CheckinFormError as error:
        context = await checkin.build_checkin_context(result, None)
        context["error"] = str(error)
        context["error_field"] = error.field
        context["room_query"] = room.strip()
        # Введённое возвращается в форму: ошибка под одним полем не стирает остальные.
        context["form_values"] = {
            "nights": nights,
            "nights_custom": nights_custom.strip(),
            "guests": guests,
            "guests_custom": guests_custom.strip(),
        }
        return await pages.hotel_page(result, "checkin.html", status_code=422, **context)
    timing = await checkin.hotel_timing(result)
    try:
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=form.room_number,
                check_out_at=checkin.checkout_at_utc(timing, form.nights, now=utc_now()),
                guests_count=form.guests_count,
            ),
            acting_user=result.actor(),
        )
    except AppError as error:
        if error.code != guests_api.ERR_GUESTS_ROOM_OCCUPIED:
            raise
        context = await checkin.build_checkin_context(
            result, form.room_number, flash="Комната уже заселена — вот её карточка."
        )
        return await pages.hotel_page(result, "checkin.html", status_code=409, **context)
    bind_token = await guests_api.issue_bind_link(checked.stay.id)
    logger.info(
        "staff.stay_checked_in",
        stay_id=str(checked.stay.id),
        room_number=checked.stay.room_number,
        guests_count=checked.stay.guests_count,
        user_id=str(result.user_id),
    )
    record_staff_checkin()
    context = await checkin.build_checkin_context(result, None)
    context["card"] = await checkin.stay_card(
        result,
        checked.stay,
        timing.zone,
        access_code=checked.access_code,
        bind_token=bind_token,
        timing=timing,
    )
    return await pages.hotel_page(result, "checkin.html", **context)
