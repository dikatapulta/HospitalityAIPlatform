"""Страница входа суперадмина `GET/POST /staff/{tenant_slug}/superadmin` (spec 0044 §3).

Дверь внутрь, как `login.py`: без сессии, тенант — из пути. Ссылок на страницу
нет нигде — это закладка основателя; обычная страница входа суперадмина не
пускает (`staff_auth.login`). Логин, пароль и код проверяются разом, любой
отказ — один текст «Неверный логин, пароль или код» (ERR-AUTH-001).

Успешный вход ведёт себя как любой вход (spec 0037 §3): cookie сессии и отеля,
прежняя сессия браузера отозвана. Дальше — «Сотрудники»: суперадмин заходит
ради двух своих кнопок. CSRF-щит формы — `browser.is_cross_origin`.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.platform import staff_auth, staff_superadmin
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE, StaffHotel
from hospitality.shared.clientip import client_ip
from hospitality.shared.errors import AppError
from hospitality.staff_portal import browser
from hospitality.staff_portal.login import unknown_hotel
from hospitality.staff_portal.rendering import render_page

router = APIRouter(tags=["staff-portal"])

# Тексты отказов страницы (spec 0044, показаны основателю в чате 24.10): на
# этой двери ERR-AUTH-001 называет и код — иначе верный пароль с опечаткой в
# коде выглядел бы как забытый пароль.
_REJECTED: Final = "Неверный логин, пароль или код."
_EMPTY: Final = "Введите логин, пароль и код."


def superadmin_login_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/superadmin"


def _form(
    hotel: StaffHotel,
    *,
    other_session: bool,
    staff_login: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return browser.html_page(
        render_page(
            "superadmin_login.html",
            tenant_name=hotel.name,
            action=superadmin_login_path(hotel.slug),
            staff_login_url=browser.hotel_login_path(hotel.slug),
            other_session=other_session,
            staff_login=staff_login,
            error=error,
        ),
        status_code=status_code,
    )


async def _has_live_session(request: Request) -> bool:
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    return bool(token) and await staff_auth.resolve_staff_session(token or "") is not None


@router.get(
    "/{tenant_slug}/superadmin", response_class=HTMLResponse, summary="Вход суперадмина: форма"
)
async def superadmin_login_page(request: Request, tenant_slug: str) -> Response:
    hotel = await staff_auth.find_hotel(tenant_slug)
    if hotel is None:
        return unknown_hotel(tenant_slug)
    return _form(hotel, other_session=await _has_live_session(request))


@router.post(
    "/{tenant_slug}/superadmin",
    response_class=HTMLResponse,
    summary="Вход суперадмина: логин + пароль + код из приложения",
)
async def superadmin_login_submit(
    request: Request,
    tenant_slug: str,
    login: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
) -> Response:
    """Успех → cookie, прежняя сессия браузера отозвана, 303 на «Сотрудники»;
    отказ → та же форма с текстом. Дефолты полей пустые — урезанный POST
    получает форму, а не JSON-конверт 422 (канон `login.py`)."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    hotel = await staff_auth.find_hotel(tenant_slug)
    if hotel is None:
        return unknown_hotel(tenant_slug)
    previous_token = request.cookies.get(STAFF_SESSION_COOKIE)
    if not login.strip() or not password or not code.strip():
        error, status_code = _EMPTY, 422
    else:
        try:
            grant = await staff_superadmin.superadmin_login(
                hotel.tenant_id, login, password, code, client_ip=client_ip(request)
            )
        except AppError as rejected:
            error = (
                _REJECTED
                if rejected.code == staff_auth.ERR_AUTH_INVALID_CREDENTIALS
                else browser.AUTH_ERROR_MESSAGES.get(rejected.code, _REJECTED)
            )
            status_code = rejected.status_code
        else:
            if previous_token:
                await staff_auth.logout(previous_token)
            response: Response = RedirectResponse(f"/staff/{hotel.slug}/team", status_code=303)
            browser.set_session_cookie(response, grant.session_token)
            browser.set_hotel_cookie(response, hotel.slug)
            return response
    return _form(
        hotel,
        other_session=await _has_live_session(request),
        staff_login=login.strip(),
        error=error,
        status_code=status_code,
    )
