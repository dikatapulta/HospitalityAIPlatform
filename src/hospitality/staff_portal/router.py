"""Страницы кабинета персонала: вход, выбор отеля, каркас (spec 0033 §3.3/§4).

Server-rendered внутри монолита (ADR-014): Jinja2-шаблоны + мобильный
CSS-канон, без JS-сборки. Контекст тенанта для страниц под
`/staff/{tenant_slug}/…` ставит звено `TenantResolver`
(`platform/auth.resolve_tenant_from_staff_session`); авторизацию действия
выполняет сама страница (`_page_context` поверх `staff_auth.require_role`).

Cookie сессии (контракт `STAFF_SESSION_COOKIE`, ревью PR #148): HttpOnly +
Secure + SameSite=Lax + Path=/staff. CSRF-контракт кабинета:

- SameSite=Lax отрезает кросс-сайтовые POST с cookie; GET не меняют данных
  (логаут — POST), поэтому top-level навигация ничего не меняет. Единственный
  побочный эффект GET — продление idle-TTL сессии (`last_used_at`), для CSRF
  он безвреден.
- Оборона в глубину — `_is_cross_origin`: заголовок `Origin` не совпал с
  `Host` → 403 ещё до чтения формы. Закрывает и login-CSRF (вход в чужую
  учётку атакующего), где session-cookie ещё нет и SameSite не помогает.
  ЗАПРОС БЕЗ Origin допускается только для HTML-форм (curl, старые браузеры);
  остаточный login-CSRF при вырезанном Origin — принятый риск v1.
- JSON-действия (PR D: взять/готово/отменить, `api_router.py`) обязаны
  требовать `Content-Type: application/json` И НЕПУСТОЙ same-origin `Origin`
  (fetch шлёт его всегда): кросс-сайтовая форма не умеет JSON-тип,
  кросс-сайтовый fetch не пройдёт по Origin. Отдельный CSRF-токен не нужен,
  пока действия соблюдают оба правила. Нарушение — 403 `ERR-AUTH-009`.

HTML кабинета аутентифицирован и не кэшируется (`_PAGE_HEADERS`: no-store,
запрет фреймов, no-referrer) — страницы живут на личных телефонах за
Cloudflare, а с PR D несут тексты заявок гостей. CSP — `default-src 'self'`
(рекомендация ревью PR #153): собственный JS очереди — отдельный файл
`static/queue.js`, inline-скрипты и любые чужие источники запрещены.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE, StaffContext
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal.api_router import router as api_router
from hospitality.staff_portal.queue import build_queue_context, parse_queue_tab
from hospitality.staff_portal.rendering import (
    QUEUE_JS,
    QUEUE_JS_VERSION,
    STYLES_CSS,
    STYLES_VERSION,
    render_page,
)

logger = get_logger(module=__name__)

router = APIRouter(prefix="/staff", tags=["staff-portal"])

_ROLE_LABELS: Final[dict[StaffRole, str]] = {
    StaffRole.STAFF: "Сотрудник",
    StaffRole.RECEPTIONIST: "Ресепшен",
    StaffRole.MANAGER: "Менеджер",
}


def _role_label(role: StaffRole) -> str:
    # Фолбэк на value: новая роль без перевода не должна ронять страницу 500-кой.
    return _ROLE_LABELS.get(role, role.value)


# Аутентифицированный HTML: не кэшировать нигде, не встраивать во фреймы
# (clickjacking на кнопках действий), не отдавать referrer наружу. CSP
# default-src 'self' — свой JS только файлом из /staff/static, inline и чужие
# источники запрещены (ревью PR #153: ужесточено с появлением своего JS).
_PAGE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}


def _html_page(html: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(html, status_code=status_code, headers=_PAGE_HEADERS)


# Сообщения формы входа по кодам каталога ошибок: пользователю — по-русски и
# без деталей (перечисление email запрещено — один текст на оба отказа).
_LOGIN_ERROR_MESSAGES: Final[dict[str, str]] = {
    staff_auth.ERR_AUTH_INVALID_CREDENTIALS: "Неверный email или пароль.",
    staff_auth.ERR_AUTH_USER_DEACTIVATED: (
        "Учётная запись деактивирована — обратитесь к менеджеру."
    ),
    ERR_AUTH_LOGIN_RATE_LIMITED: (
        "Слишком много попыток входа. Подождите несколько минут и попробуйте снова."
    ),
}

# Разделы главной по мини-матрице spec 0033 §3.2. Очередь — живая ссылка
# (PR D); заселение и сотрудники — «скоро» (PR E, PR F).
_SECTION_CHECKIN: Final = {"title": "Заселение", "note": "Скоро"}
_SECTION_TEAM: Final = {"title": "Сотрудники", "note": "Скоро"}

_any_role: Final = staff_auth.require_role(
    StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_cross_origin(request: Request) -> bool:
    """CSRF-щит форм и будущих JSON-действий (см. докстринг модуля).

    `Origin` шлют все современные браузеры на POST; отсутствие заголовка
    (curl, смоук-тесты) не считается кросс-сайтом — это оборона в глубину
    поверх SameSite=Lax, а не единственная граница.
    """
    origin = request.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc != request.headers.get("host", "")


def _cross_origin_rejected(request: Request) -> Response:
    # Браузерная граница до чтения формы — вне конверта ошибок R-8 (осознанно):
    # это не API-ответ клиенту, а отказ навигации; диагноз — по лог-событию.
    logger.warning("staff.cross_origin_rejected", origin=request.headers.get("origin", ""))
    return PlainTextResponse("Запрос отклонён: чужой источник (CSRF).", status_code=403)


def _set_session_cookie(response: Response, token: str) -> None:
    """Cookie сессии кабинета — контракт ревью PR #148 (spec 0033 §3.3).

    Max-Age = absolute TTL: idle-срок и отзыв всё равно проверяет сервер на
    каждом запросе (`resolve_staff_session`) — атрибуты cookie не граница
    безопасности, как и в гостевом канале.
    """
    response.set_cookie(
        STAFF_SESSION_COOKIE,
        token,
        max_age=get_settings().staff_session_absolute_ttl_days * 86400,
        path="/staff",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        STAFF_SESSION_COOKIE, path="/staff", httponly=True, secure=True, samesite="lax"
    )


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


@router.get("/static/styles.css", include_in_schema=False)
async def styles() -> Response:
    """Мобильный CSS-канон из памяти; кэш — навсегда, версию меняет URL."""
    return Response(
        STYLES_CSS,
        media_type="text/css; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{STYLES_VERSION}"',
        },
    )


@router.get("/static/queue.js", include_in_schema=False)
async def queue_js() -> Response:
    """Ванильный JS очереди (поллинг + действия) — тот же режим, что styles.css:
    из памяти, кэш навсегда, версию меняет URL; CSP пускает только свой файл."""
    return Response(
        QUEUE_JS,
        media_type="text/javascript; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{QUEUE_JS_VERSION}"',
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
    if _is_cross_origin(request):
        return _cross_origin_rejected(request)
    if not email or not password:
        return _html_page(
            render_page("login.html", error="Введите email и пароль.", email=email),
            status_code=422,
        )
    try:
        grant = await staff_auth.login(email, password, client_ip=_client_ip(request))
    except AppError as error:
        message = _LOGIN_ERROR_MESSAGES.get(error.code, "Не получилось войти. Попробуйте ещё раз.")
        return _html_page(
            render_page("login.html", error=message, email=email),
            status_code=error.status_code,
        )
    if len(grant.memberships) == 1:
        target = f"/staff/{grant.memberships[0].tenant_slug}"
    else:
        target = "/staff/"
    response: Response = RedirectResponse(target, status_code=303)
    _set_session_cookie(response, grant.session_token)
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
                    "role_label": _role_label(membership.role_key),
                }
                for membership in memberships
            ],
        )
    )


@router.post("/logout", summary="Выход: погасить сессию и cookie")
async def logout_submit(request: Request) -> Response:
    if _is_cross_origin(request):
        return _cross_origin_rejected(request)
    token = request.cookies.get(STAFF_SESSION_COOKIE)
    if token:
        await staff_auth.logout(token)
    response: Response = RedirectResponse("/staff/login", status_code=303)
    _clear_session_cookie(response)
    return response


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
        sections.append(_SECTION_CHECKIN)
    if result.role_key is StaffRole.MANAGER:
        sections.append(_SECTION_TEAM)
    return _html_page(
        render_page(
            "home.html",
            display_name=result.display_name,
            tenant_name=result.tenant_name,
            role_label=_role_label(result.role_key),
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


# JSON-действия очереди (api_router.py) — в конце: служебные литеральные
# маршруты (/login, /logout, /static/*) обязаны регистрироваться раньше
# шаблонных путей с {tenant_slug} (порядок — контракт README пакета).
router.include_router(api_router)
