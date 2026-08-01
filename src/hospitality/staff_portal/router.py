"""Страницы кабинета персонала: вход, выбор отеля, разделы (spec 0033 §3.3/§4).

Server-rendered внутри монолита (ADR-014): Jinja2-шаблоны + мобильный
CSS-канон, без JS-сборки. Контекст тенанта для страниц под
`/staff/{tenant_slug}/…` ставит звено `TenantResolver`
(`platform/auth.resolve_tenant_from_staff_session`); авторизацию действия
выполняет сама страница (`_page_context` поверх `staff_auth.require_role`).
Анонимные страницы приглашения (`/staff/invite/{token}`) живут отдельным
роутером (`invites.py`) — у них нет ни сессии, ни тенанта; общая браузерная
обвязка обоих роутеров (заголовки, CSRF-щит форм, cookie) — `browser.py`.

Cookie сессии (контракт `STAFF_SESSION_COOKIE`, ревью PR #148): HttpOnly +
Secure + SameSite=Lax + Path=/staff. CSRF-контракт кабинета:

- SameSite=Lax отрезает кросс-сайтовые POST с cookie; GET не меняют данных
  (логаут — POST), поэтому top-level навигация ничего не меняет. Единственный
  побочный эффект GET — продление idle-TTL сессии (`last_used_at`), для CSRF
  он безвреден.
- Оборона в глубину — `browser.is_cross_origin`: заголовок `Origin` не совпал
  с `Host` → 403 ещё до чтения формы. Закрывает и login-CSRF (вход в чужую
  учётку атакующего), где session-cookie ещё нет и SameSite не помогает —
  тот же щит стоит на форме принятия приглашения, которая тоже создаёт сессию.
  ЗАПРОС БЕЗ Origin допускается только для HTML-форм (curl, старые браузеры);
  остаточный login-CSRF при вырезанном Origin — принятый риск v1.
- JSON-действия (очередь, карточка Stay, состав команды — `api_router.py`)
  обязаны требовать `Content-Type: application/json` И НЕПУСТОЙ same-origin
  `Origin` (fetch шлёт его всегда): кросс-сайтовая форма не умеет JSON-тип,
  кросс-сайтовый fetch не пройдёт по Origin. Отдельный CSRF-токен не нужен,
  пока действия соблюдают оба правила. Нарушение — 403 `ERR-AUTH-009`.

HTML кабинета аутентифицирован и не кэшируется (`browser.PAGE_HEADERS`:
no-store, запрет фреймов, no-referrer) — страницы живут на личных телефонах за
Cloudflare и несут тексты заявок гостей. CSP — `default-src 'self'`
(рекомендация ревью PR #153): собственный JS страниц — отдельные файлы
`static/*.js`, inline-скрипты и любые чужие источники запрещены.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.modules.guests import api as guests_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE, StaffContext
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_checkin
from hospitality.staff_portal import browser, checkin, team
from hospitality.staff_portal.api_router import router as api_router
from hospitality.staff_portal.browser import html_page as _html_page
from hospitality.staff_portal.invites import router as invite_router
from hospitality.staff_portal.queue import build_queue_context, parse_queue_tab
from hospitality.staff_portal.rendering import STATIC_ASSETS, render_page

logger = get_logger(module=__name__)

router = APIRouter(prefix="/staff", tags=["staff-portal"])

_any_role: Final = staff_auth.require_role(
    StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER
)
# Заселение и карточки Stay — ресепшен и менеджер (мини-матрица §3.2).
_reception_role: Final = staff_auth.require_role(StaffRole.RECEPTIONIST, StaffRole.MANAGER)
# Сотрудники — только менеджер (мини-матрица §3.2, развилка Ф-3 spec 0033).
_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


async def _page_context(
    request: Request, role_dependency: Callable[[Request], Awaitable[StaffContext]]
) -> StaffContext | Response:
    """Канон авторизации страницы кабинета (P-12; PR E/F копируют).

    Та же проверка, что у JSON-действий (`require_role`), но исходы —
    браузерные: 401 (нет/истекла сессия) → редирект на логин, 403 (нет
    членства/роли, несовпадение RLS-контекста) → HTML «нет доступа» вместо
    JSON-конверта. Сверку RLS-контекста запроса с тенантом страницы выполняет
    сам `require_role` (fail-closed, ревью PR #153) — и страницы, и
    JSON-действия под одной защитой. На 403 сессия заведомо жива (иначе был
    бы 401) — страница показывает «Выйти».
    """
    try:
        return await role_dependency(request)
    except AppError as error:
        if error.status_code == 401:
            return RedirectResponse("/staff/login", status_code=303)
        return _html_page(render_page("forbidden.html", show_logout=True), status_code=403)


@router.get("/static/{filename}", include_in_schema=False)
async def static_asset(filename: str) -> Response:
    """Статика кабинета (CSS-канон и ванильный JS страниц) — из памяти, кэш
    навсегда, версию меняет URL; CSP пускает только эти файлы.

    Один маршрут на все файлы (PR F): четвёртая копия одного и того же
    обработчика ради `team.js` — ровно то дублирование, которое запрещает
    P-12. Список — `rendering.STATIC_ASSETS`, чужого имени здесь не отдать.
    """
    asset = STATIC_ASSETS.get(filename)
    if asset is None:
        # 404 каноническим конвертом (ERR-PLATFORM-003) отдаёт обработчик
        # HTTPException приложения — своего ответа у статики нет.
        raise HTTPException(status_code=404, detail="Unknown static asset")
    return Response(
        asset.content,
        media_type=asset.media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{asset.version}"',
        },
    )


@router.get("/login", response_class=HTMLResponse, summary="Форма входа персонала")
async def login_page(request: Request) -> Response:
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    if token is not None and await staff_auth.resolve_staff_session(token) is not None:
        return RedirectResponse("/staff/", status_code=303)
    return _html_page(render_page("login.html"))


@router.post(
    "/login",
    response_class=HTMLResponse,
    summary="Вход: email + пароль → сессия кабинета",
)
async def login_submit(
    request: Request,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Успех → cookie + редирект (spec 0033 §3.3: одно членство — сразу в
    тенанта, иначе — экран выбора); отказ → та же форма с текстом ошибки и
    статусом отказа. Дефолты полей пустые: браузер их всегда шлёт, а урезанный
    ручной POST должен получить ту же HTML-форму, а не JSON-конверт 422."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    if not email or not password:
        return _html_page(
            render_page("login.html", error="Введите email и пароль.", email=email),
            status_code=422,
        )
    try:
        grant = await staff_auth.login(email, password, client_ip=browser.client_ip(request))
    except AppError as error:
        message = browser.AUTH_ERROR_MESSAGES.get(
            error.code, "Не получилось войти. Попробуйте ещё раз."
        )
        return _html_page(
            render_page("login.html", error=message, email=email),
            status_code=error.status_code,
        )
    if len(grant.memberships) == 1:
        target = f"/staff/{grant.memberships[0].tenant_slug}"
    else:
        target = "/staff/"
    response: Response = RedirectResponse(target, status_code=303)
    browser.set_session_cookie(response, grant.session_token)
    return response


@router.get("/", response_class=HTMLResponse, summary="Выбор отеля (членства сотрудника)")
async def select_tenant(request: Request) -> Response:
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    active = await staff_auth.resolve_staff_session(token) if token else None
    if active is None:
        return RedirectResponse("/staff/login", status_code=303)
    memberships = await staff_auth.list_memberships(active.user_id)
    return _html_page(
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
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    if token:
        await staff_auth.logout(token)
    response: Response = RedirectResponse("/staff/login", status_code=303)
    browser.clear_session_cookie(response)
    return response


# Анонимные страницы приглашения (`/staff/invite/{token}`) — ДО шаблонных
# путей с {tenant_slug}: `invite` служебный сегмент кабинета, не slug отеля
# (он же перечислен в `_STAFF_RESERVED_SEGMENTS` резолвера тенанта).
router.include_router(invite_router)


@router.get(
    "/{tenant_slug}",
    response_class=HTMLResponse,
    summary="Главная кабинета отеля (каркас v1)",
)
async def home(request: Request, tenant_slug: str) -> Response:
    del tenant_slug  # slug читает require_role из path_params (канон staff_auth)
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    sections: list[dict[str, str]] = [
        {"title": "Очередь заявок", "href": f"/staff/{result.tenant_slug}/requests"}
    ]
    if result.role_key in (StaffRole.RECEPTIONIST, StaffRole.MANAGER):
        sections.append({"title": "Заселение", "href": f"/staff/{result.tenant_slug}/checkin"})
    if result.role_key is StaffRole.MANAGER:
        sections.append({"title": "Сотрудники", "href": f"/staff/{result.tenant_slug}/team"})
    return _html_page(
        render_page(
            "home.html",
            display_name=result.display_name,
            tenant_name=result.tenant_name,
            role_label=team.role_label(result.role_key),
            sections=sections,
        )
    )


@router.get(
    "/{tenant_slug}/requests",
    response_class=HTMLResponse,
    summary="Очередь заявок (spec 0033 §5, закрывает #56)",
)
async def requests_queue(request: Request, tenant_slug: str) -> Response:
    """Лента открытых (`new`+`in_progress`) или «закрытые за сегодня»;
    фильтры-чипсы — категория и «мои». Действия — JSON-эндпоинты
    `api_router.py`, обновление — поллинг `static/queue.js` каждые 15 с."""
    del tenant_slug
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    context = await build_queue_context(
        result,
        tab=parse_queue_tab(request.query_params.get("tab")),
        category_key=request.query_params.get("category") or None,
        mine=request.query_params.get("mine") == "1",
    )
    return _html_page(render_page("queue.html", **context))


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
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    context = await build_queue_context(
        result,
        tab=parse_queue_tab(request.query_params.get("tab")),
        category_key=request.query_params.get("category") or None,
        mine=request.query_params.get("mine") == "1",
    )
    return _html_page(render_page("_queue_list.html", **context))


@router.get(
    "/{tenant_slug}/checkin",
    response_class=HTMLResponse,
    summary="Страница заселения (spec 0033 §6)",
)
async def checkin_page(request: Request, tenant_slug: str) -> Response:
    """Форма «комната → ночи → гости → Заселить»; `?room=` открывает карточку
    занятой комнаты (или ту же форму с подсказкой «свободна»)."""
    del tenant_slug
    result = await _page_context(request, _reception_role)
    if isinstance(result, Response):
        return result
    context = await checkin.build_checkin_context(result, request.query_params.get("room"))
    return _html_page(render_page("checkin.html", **context))


@router.post(
    "/{tenant_slug}/checkin",
    response_class=HTMLResponse,
    summary="Заселить: Guest + Stay + код + QR bind-ссылки",
)
async def checkin_submit(
    request: Request,
    tenant_slug: str,
    room: Annotated[str, Form()] = "",
    nights: Annotated[str, Form()] = "1",
    nights_custom: Annotated[str, Form()] = "",
    guests: Annotated[str, Form()] = "1",
) -> Response:
    """HTML-форма (канон CSRF — как логин: Origin-проверка + SameSite). Успех
    рендерится СРАЗУ, без redirect: код заселения показывается ровно один раз.
    Повторный submit той же комнаты (refresh) — не дубль, а карточка занятой
    комнаты (ERR-GUESTS-002 держит БД). Дефолты полей — как у логина: урезанный
    POST получает HTML-форму, а не 422-конверт."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    del tenant_slug
    result = await _page_context(request, _reception_role)
    if isinstance(result, Response):
        return result
    try:
        form = checkin.parse_checkin_form(room, nights, nights_custom, guests)
    except checkin.CheckinFormError as error:
        context = await checkin.build_checkin_context(result, None)
        context["error"] = str(error)
        context["room_query"] = room.strip()
        return _html_page(render_page("checkin.html", **context), status_code=422)
    zone = await checkin.hotel_zone(result)
    try:
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=form.room_number,
                check_out_at=checkin.checkout_at_utc(zone, form.nights, now=utc_now()),
                guests_count=form.guests_count,
            )
        )
    except AppError as error:
        if error.code != guests_api.ERR_GUESTS_ROOM_OCCUPIED:
            raise
        context = await checkin.build_checkin_context(
            result, form.room_number, flash="Комната уже заселена — вот её карточка."
        )
        return _html_page(render_page("checkin.html", **context), status_code=409)
    flash = None
    try:
        bind_token: str | None = await guests_api.issue_bind_link(checked.stay.id)
    except AppError as error:
        if error.code != guests_api.ERR_GUESTS_BINDLINK_UNAVAILABLE:
            raise
        # Redis лёг — заселение уже случилось, карточка без QR честнее 500-ки.
        bind_token = None
        flash = "QR-ссылка сейчас недоступна — передайте гостю код с карточки."
    logger.info(
        "staff.stay_checked_in",
        stay_id=str(checked.stay.id),
        room_number=checked.stay.room_number,
        guests_count=checked.stay.guests_count,
        user_id=str(result.user_id),
    )
    record_staff_checkin()
    context = await checkin.build_checkin_context(result, None, flash=flash)
    context["card"] = await checkin.stay_card(
        result, checked.stay, zone, access_code=checked.access_code, bind_token=bind_token
    )
    return _html_page(render_page("checkin.html", **context))


@router.get(
    "/{tenant_slug}/team",
    response_class=HTMLResponse,
    summary="Сотрудники: состав, приглашения, роли (spec 0033 §7)",
)
async def team_page(request: Request, tenant_slug: str) -> Response:
    """Список членств отеля + форма приглашения + ожидающие ссылки.
    Роль `manager` (мини-матрица §3.2); действия — JSON-эндпоинты
    `api_router.py`, ссылка приглашения показывается один раз из `team.js`."""
    del tenant_slug
    result = await _page_context(request, _manager_role)
    if isinstance(result, Response):
        return result
    return _html_page(render_page("team.html", **await team.build_team_context(result)))


# JSON-действия кабинета (api_router.py) — в конце: служебные литеральные
# маршруты (/login, /logout, /static/*, /invite/*) обязаны регистрироваться
# раньше шаблонных путей с {tenant_slug} (порядок — контракт README пакета).
router.include_router(api_router)
