"""Вход и выход кабинета: общий вход по коду отеля, вход отеля, выбор, выход (spec 0037 §3).

Вход — со страницы своего отеля `/staff/{tenant_slug}/login`: логин уникален
только внутри отеля (spec 0037 §2), поэтому отель выбирается до логина, а не
после. Помнить адрес своего отеля сотрудник не обязан — общий вход
`/staff/login` ведёт туда по коду отеля (это slug тенанта) или по cookie
`staff_hotel`, которую ставит успешный вход (`browser.set_hotel_cookie`).

Цепочка `GET /staff/login` (порядок — контракт spec 0037 §3):

1. `?hotel=<код>` — разбирается первым и без оглядки на cookie (иначе «Другой
   отель» возвращал бы на прежний): найден → 303 на вход отеля, нет → форма
   кода с ошибкой;
2. `?change=1` — форма кода, cookie не смотрится по той же причине;
3. cookie с существующим отелем → 303 на вход отеля;
4. иначе — форма кода.

Сессия у человека в двух отелях — два User, а cookie сессии одна: поэтому вход
отеля при живой сессии ведёт в кабинет только при членстве в ЭТОМ отеле и без
`?switch=1`, а успешный вход отзывает прежнюю сессию браузера (иначе она
дожила бы до TTL без владельца). Неуспешный вход её не трогает.

Маршруты регистрируются раньше шаблонных путей `/{tenant_slug}` (порядок —
контракт README пакета); CSRF-щит форм и cookie — `browser.py`. `/staff` без
слэша живёт в `router.py`: путь «пустой» в роутере без префикса FastAPI не
регистрирует.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.platform import staff_auth
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE, StaffHotel
from hospitality.shared.clientip import client_ip
from hospitality.shared.errors import AppError
from hospitality.staff_portal import browser, team
from hospitality.staff_portal.rendering import render_page

router = APIRouter(tags=["staff-portal"])

_UNKNOWN_HOTEL = (
    "Отель с таким кодом не найден. Код пишется латиницей — проверьте раскладку "
    "или спросите менеджера."
)


def _code_form(
    *, hotel_code: str = "", error: str | None = None, status_code: int = 200
) -> Response:
    return browser.html_page(
        render_page("hotel_code.html", hotel_code=hotel_code, error=error),
        status_code=status_code,
    )


def _unknown_hotel(hotel_code: str) -> Response:
    """404 неизвестного отеля — формой кода с ошибкой, а не JSON-конвертом:
    сюда попадают руками набранный адрес и ссылка с опечаткой (spec 0037 §3)."""
    return _code_form(hotel_code=hotel_code, error=_UNKNOWN_HOTEL, status_code=404)


def _login_form(
    hotel: StaffHotel,
    *,
    other_session: bool,
    staff_login: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return browser.html_page(
        render_page(
            "login.html",
            tenant_name=hotel.name,
            action=browser.hotel_login_path(hotel.slug),
            other_session=other_session,
            staff_login=staff_login,
            error=error,
        ),
        status_code=status_code,
    )


def _redirect(path: str) -> Response:
    return RedirectResponse(path, status_code=303)


async def _remembered_hotel(request: Request) -> StaffHotel | None:
    """Отель из cookie `staff_hotel`, если он ещё существует."""
    slug = request.cookies.get(browser.STAFF_HOTEL_COOKIE)
    return await staff_auth.find_hotel(slug) if slug else None


async def _live_session(request: Request) -> staff_auth.ActiveStaffUser | None:
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    return await staff_auth.resolve_staff_session(token) if token else None


@router.get("/login", response_class=HTMLResponse, summary="Общий вход: код отеля")
async def hotel_code_page(request: Request) -> Response:
    """Один адрес входа для всех отелей (spec 0037 §3; цепочка — докстринг модуля).

    Код отеля — это slug: ввод приводится к нему trim + нижний регистр, поэтому
    slug заводится строчным (рунбук онбординга). Форма отправляется GET сюда
    же, данных не меняет — CSRF-щит не нужен."""
    hotel_code = request.query_params.get("hotel")
    if hotel_code is not None:
        if not hotel_code.strip():
            return _code_form()
        hotel = await staff_auth.find_hotel(hotel_code.strip().lower())
        if hotel is None:
            return _unknown_hotel(hotel_code.strip())
        return _redirect(browser.hotel_login_path(hotel.slug))
    if request.query_params.get("change") != "1":
        hotel = await _remembered_hotel(request)
        if hotel is not None:
            return _redirect(browser.hotel_login_path(hotel.slug))
    return _code_form()


@router.post("/login", include_in_schema=False)
async def legacy_login_submit() -> Response:
    """Форма email + пароль, открытая до перехода на логины (вкладка из дня
    деплоя), отправляет POST сюда: вместо JSON-конверта 405 — общий вход.
    Данных не читает и не меняет, поэтому CSRF-щит не нужен."""
    return _redirect("/staff/login")


@router.get(
    "/{tenant_slug}/login", response_class=HTMLResponse, summary="Вход отеля: логин + пароль"
)
async def hotel_login_page(request: Request, tenant_slug: str) -> Response:
    """Живая сессия с членством в этом отеле и без `?switch=1` → кабинет;
    живая сессия иначе → форма со строкой «открыт кабинет под другим
    логином»; без сессии признак ничего не меняет (spec 0037 §3)."""
    hotel = await staff_auth.find_hotel(tenant_slug)
    if hotel is None:
        return _unknown_hotel(tenant_slug)
    active = await _live_session(request)
    if (
        active is not None
        and request.query_params.get("switch") != "1"
        and await staff_auth.load_staff_context(active, hotel.slug) is not None
    ):
        return _redirect(f"/staff/{hotel.slug}")
    return _login_form(hotel, other_session=active is not None)


@router.post(
    "/{tenant_slug}/login", response_class=HTMLResponse, summary="Вход: логин + пароль → сессия"
)
async def hotel_login_submit(
    request: Request,
    tenant_slug: str,
    login: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Успех → cookie сессии и отеля, прежняя сессия браузера отозвана, 303 в
    кабинет отеля; отказ → та же форма с текстом ошибки и статусом отказа.
    Дефолты полей пустые: урезанный ручной POST получает HTML-форму, а не
    JSON-конверт 422."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    hotel = await staff_auth.find_hotel(tenant_slug)
    if hotel is None:
        return _unknown_hotel(tenant_slug)
    previous_token = request.cookies.get(STAFF_SESSION_COOKIE)
    if not login.strip() or not password:
        error: str | None = "Введите логин и пароль."
        status_code = 422
    else:
        try:
            grant = await staff_auth.login(
                hotel.tenant_id, login, password, client_ip=client_ip(request)
            )
        except AppError as rejected:
            error = browser.AUTH_ERROR_MESSAGES.get(
                rejected.code, "Не получилось войти. Попробуйте ещё раз."
            )
            status_code = rejected.status_code
        else:
            if previous_token:
                await staff_auth.logout(previous_token)
            response = _redirect(f"/staff/{hotel.slug}")
            browser.set_session_cookie(response, grant.session_token)
            browser.set_hotel_cookie(response, hotel.slug)
            return response
    return _login_form(
        hotel,
        other_session=await _live_session(request) is not None,
        staff_login=login.strip(),
        error=error,
        status_code=status_code,
    )


@router.get(
    "/", response_class=HTMLResponse, summary="Кабинет своего отеля (выбор — если их несколько)"
)
async def select_tenant(request: Request) -> Response:
    """Одно активное членство → 303 в кабинет: выбора как шага нет (spec 0037
    §3). Список остаётся старым учёткам с несколькими членствами."""
    active = await _live_session(request)
    if active is None:
        return _redirect("/staff/login")
    memberships = await staff_auth.list_memberships(active.user_id)
    if len(memberships) == 1:
        return _redirect(f"/staff/{memberships[0].tenant_slug}")
    return browser.html_page(
        render_page(
            "select_tenant.html",
            display_name=active.display_name,
            memberships=[
                {
                    "tenant_slug": membership.tenant_slug,
                    "tenant_name": membership.tenant_name,
                    "role_label": team.role_label(membership.role_key),
                }
                for membership in memberships
            ],
        )
    )


@router.post("/logout", summary="Выход: погасить сессию и cookie")
async def logout_submit(request: Request) -> Response:
    """Выход ведёт на вход отеля из cookie `staff_hotel`, без неё — на общий
    вход; cookie отеля остаётся (spec 0037 §3)."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    if token:
        await staff_auth.logout(token)
    hotel = await _remembered_hotel(request)
    response = _redirect(browser.hotel_login_path(hotel.slug) if hotel else "/staff/login")
    browser.clear_session_cookie(response)
    return response
