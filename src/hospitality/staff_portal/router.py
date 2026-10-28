"""Страницы кабинета персонала: разделы отеля (spec 0033 §3.3/§4).

Server-rendered внутри монолита (ADR-014): Jinja2-шаблоны + мобильный
CSS-канон, без JS-сборки. Контекст тенанта для страниц под
`/staff/{tenant_slug}/…` ставит звено `TenantResolver`
(`platform/auth.resolve_tenant_from_staff_session`); авторизацию действия
выполняет сама страница (`_page_context` поверх `staff_auth.require_role`).
Вход, выбор отеля и выход живут отдельным роутером (`login.py`, spec 0037
§3), анонимные страницы приглашения (`/staff/invite/{token}`) — своим
(`invites.py`); общая браузерная обвязка всех трёх (заголовки, CSRF-щит
форм, cookie) — `browser.py`.

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
no-store, запрет фреймов, `Referrer-Policy: strict-origin` — наружу уходит
источник без пути, а `no-referrer` обнулял бы `Origin` у форм и ронял вход,
#164) — страницы живут на личных телефонах за
Cloudflare и несут тексты заявок гостей. CSP — `default-src 'self'`
(рекомендация ревью PR #153): собственный JS страниц — отдельные файлы
`static/*.js`, inline-скрипты и любые чужие источники запрещены.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.modules.documents import api as documents_api
from hospitality.modules.guests import api as guests_api
from hospitality.platform import staff_auth
from hospitality.platform.files import file_response
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import TENANT_SLUG_PATH_PARAM, StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_checkin
from hospitality.staff_portal import (
    act_requests,
    browser,
    changes,
    checkin,
    demo_handover,
    demo_orders,
    knowledge,
    new_request,
    scope,
    shell,
    signal,
    summary,
    team,
    turnovers,
)
from hospitality.staff_portal import home as home_page
from hospitality.staff_portal import queue as queue_page
from hospitality.staff_portal.act_request_api import router as act_request_api_router
from hospitality.staff_portal.api_router import router as api_router
from hospitality.staff_portal.browser import html_page as _html_page
from hospitality.staff_portal.invites import router as invite_router
from hospitality.staff_portal.login import router as login_router
from hospitality.staff_portal.password_reset import router as password_reset_router
from hospitality.staff_portal.push_api import push_context
from hospitality.staff_portal.push_api import router as push_api_router
from hospitality.staff_portal.queue import build_queue_context, parse_queue_tab
from hospitality.staff_portal.rendering import STATIC_ASSETS, render_page
from hospitality.staff_portal.superadmin import router as superadmin_login_router
from hospitality.staff_portal.turnover_api import router as turnover_api_router

logger = get_logger(module=__name__)

router = APIRouter(prefix="/staff", tags=["staff-portal"])

_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
# Заселение и карточки Stay — служба ресепшена любого уровня и менеджер (spec 0045 §2).
_reception_role: Final = staff_auth.require_role(StaffRole.MANAGER, or_reception=True)
# Сводка дня и журнал — старший (числа и строки своих служб) и менеджер (spec 0045 §3).
_senior_role: Final = staff_auth.require_role(StaffRole.SENIOR, StaffRole.MANAGER)
# Сотрудники — только менеджер (мини-матрица §3.2, развилка Ф-3 spec 0033).
_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


async def _page_context(
    request: Request, role_dependency: Callable[[Request], Awaitable[StaffContext]]
) -> StaffContext | Response:
    """Канон авторизации страницы кабинета (P-12; PR E/F копируют).

    Та же проверка, что у JSON-действий (`require_role`), но исходы —
    браузерные: 401 (нет/истекла сессия) → редирект на вход этого отеля
    (slug известен из пути, spec 0037 §3), 403 (нет членства/роли,
    несовпадение RLS-контекста) → HTML «нет доступа» вместо JSON-конверта.
    Сверку RLS-контекста запроса с тенантом страницы выполняет сам
    `require_role` (fail-closed, ревью PR #153) — и страницы, и JSON-действия
    под одной защитой. На 403 сессия заведомо жива (иначе был бы 401) —
    страница показывает «Выйти» и ссылку «войти в этот отель под другим
    логином»: признак `?switch=1` нужен из-за случая «не та роль», где
    членство есть и вход без признака вернул бы 303 в тот же кабинет.
    """
    tenant_slug = str(request.path_params[TENANT_SLUG_PATH_PARAM])
    try:
        return await role_dependency(request)
    except AppError as error:
        if error.status_code == 401:
            return RedirectResponse(browser.hotel_login_path(tenant_slug), status_code=303)
        return _forbidden_page(tenant_slug)


def _forbidden_page(tenant_slug: str) -> Response:
    """HTML «нет доступа» с выходом и входом под другим логином (канон `_page_context`)."""
    return _html_page(
        render_page(
            "forbidden.html",
            show_logout=True,
            switch_url=browser.hotel_login_path(tenant_slug, switch=True),
        ),
        status_code=403,
    )


def _hotel_page(
    staff: StaffContext, template: str, *, status_code: int = 200, **context: Any
) -> Response:
    """Страница отеля: каркас с сигналом кабинета (spec 0039 §3.1).

    Сигнал обязан звучать на КАЖДОЙ странице отеля — ЧП может прийти, пока
    ресепшен заселяет гостя, — поэтому страницы отеля рендерятся только
    отсюда: новая страница, отданная мимо, молча осталась бы без сигнала.
    Страницы входа, приглашения и «нет доступа» сюда не ходят: опрашивать
    сигнал там нечем (нет сессии или членства). Каркас с боковым меню (#462) —
    тоже только отсюда: меню строится по роли вошедшего.
    """
    return _html_page(
        render_page(
            template,
            signal=signal.signal_context(staff.tenant_slug),
            # Уведомления на телефон (spec 0050): None — пуша на контуре нет.
            push=push_context(staff.tenant_slug),
            shell=shell.shell_context(staff, template),
            **context,
        ),
        status_code=status_code,
    )


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


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Service worker веб-пуша (spec 0050 §3) — из `/staff/`, а не `/staff/static/`:
    область service worker не шире каталога его файла, а показывать уведомления
    он обязан на всём кабинете. Отдаётся и без пуша на контуре: файл статичен,
    без подписки ничего не делает, а регистрировать его без пуша некому (`push.js`
    подключается только с ключом). Кэш — `no-cache`: новая версия доходит до
    телефона при следующей проверке."""
    asset = STATIC_ASSETS["sw.js"]
    return Response(
        asset.content, media_type=asset.media_type, headers={"Cache-Control": "no-cache"}
    )


@router.get("", include_in_schema=False)
async def staff_root() -> Response:
    """`/staff` без слэша — общий адрес входа, который «Сотрудники» показывают
    персоналу (spec 0037 §5). Без своего маршрута его ловит `redirect_slashes`
    Starlette и отвечает АБСОЛЮТНЫМ адресом со схемой из запроса — за туннелем
    это `http://` (uvicorn с `--no-proxy-headers`), и Secure-cookie кабинета на
    такой странице не живут. Относительный 303 оставляет браузер на https."""
    return RedirectResponse("/staff/", status_code=303)


# Вход, выбор отеля и выход (`/login`, `/{tenant_slug}/login`, `/`, `/logout`)
# — ДО шаблонных путей с {tenant_slug}: литеральные маршруты регистрируются
# раньше шаблонных (контракт README пакета).
router.include_router(login_router)
# Вход суперадмина `/staff/{tenant_slug}/superadmin` (spec 0044 §3) — дверь
# внутрь, как вход отеля, без `_page_context`.
router.include_router(superadmin_login_router)


# Анонимные страницы приглашения (`/staff/invite/{token}`) — ДО шаблонных
# путей с {tenant_slug}: `invite` служебный сегмент кабинета, не slug отеля
# (он же перечислен в `_STAFF_RESERVED_SEGMENTS` резолвера тенанта).
router.include_router(invite_router)
# Ссылка нового пароля `/staff/reset/{token}` (spec 0044 §4) — вторая анонимная
# дверь по токену; `reset` — служебный сегмент резолвера, как `invite`.
router.include_router(password_reset_router)


@router.get(
    "/{tenant_slug}",
    response_class=HTMLResponse,
    summary="Главная кабинета отеля: разделы плитками с числами (issue #454)",
)
async def home(request: Request, tenant_slug: str) -> Response:
    """Приветствие по часам отеля и разделы роли; числа очереди и заселения
    собирает `home.build_home_context`."""
    del tenant_slug  # slug читает require_role из path_params (канон staff_auth)
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    memberships = await staff_auth.list_memberships(result.user_id)
    context = await home_page.build_home_context(result, several_hotels=len(memberships) > 1)
    return _hotel_page(result, "home.html", **context)


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
        created=request.query_params.get("created"),
        created_category_id=request.query_params.get("service"),
    )
    return _hotel_page(result, "queue.html", **context)


async def _foreign_service_page(staff: StaffContext) -> Response:
    """403 на фильтр `?category=` чужой службы (spec 0045 §5): страница отеля с
    меню и сигналом — сотрудник в своём кабинете, ему закрыта только эта лента."""
    logger.warning("staff.queue_foreign_service_denied", user_id=str(staff.user_id))
    return _hotel_page(
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
    result = await _page_context(request, _any_role)
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
    )
    return _html_page(render_page("_queue_list.html", **context))


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
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    context = await new_request.build_new_request_context(result)
    return _hotel_page(result, "new_request.html", **context)


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
    return _hotel_page(result, "checkin.html", **context)


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
        return _hotel_page(result, "checkin.html", status_code=422, **context)
    zone = await checkin.hotel_zone(result)
    try:
        checked = await guests_api.check_in(
            guests_api.StayCheckIn(
                room_number=form.room_number,
                check_out_at=checkin.checkout_at_utc(zone, form.nights, now=utc_now()),
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
        return _hotel_page(result, "checkin.html", status_code=409, **context)
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
        result, checked.stay, zone, access_code=checked.access_code, bind_token=bind_token
    )
    return _hotel_page(result, "checkin.html", **context)


@router.get(
    "/{tenant_slug}/turnovers",
    response_class=HTMLResponse,
    summary="Выезды и уборка: путь номера после выезда (spec 0046, #476)",
)
async def turnovers_page(request: Request, tenant_slug: str) -> Response:
    """Форма «Отметить выезд» ресепшену и доска номеров по шагам; видят ресепшен,
    менеджер и хаускипинг (`turnovers.rights_for`), остальным — «нет доступа».
    Раздел выключен (`ENABLE_TURNOVER_BOARD`) — 404, как у неизвестного адреса.
    Действия — `turnover_api.py`, обновление — поллинг `static/turnovers.js`."""
    if not turnovers.board_enabled():
        raise HTTPException(status_code=404)
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    if not turnovers.rights_for(result).can_view:
        return _forbidden_page(tenant_slug)
    context = await turnovers.build_turnovers_context(result, with_form=True)
    return _hotel_page(result, "turnovers.html", **context)


@router.get(
    "/{tenant_slug}/turnovers/fragment",
    response_class=HTMLResponse,
    summary="Фрагмент доски выездов (поллинг 15 с)",
)
async def turnovers_fragment(request: Request, tenant_slug: str) -> Response:
    """Доска без каркаса — JS заменяет ею контейнер; исходы — как у страницы."""
    del tenant_slug
    if not turnovers.board_enabled():
        raise HTTPException(status_code=404)
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    if not turnovers.rights_for(result).can_view:
        return Response(status_code=403)
    context = await turnovers.build_turnovers_context(result, with_form=False)
    return _html_page(render_page("_turnover_board.html", **context))


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
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    if not act_requests.can_handle_act_requests(result):
        return _forbidden_page(tenant_slug)
    context = await act_requests.build_act_requests_context(result, dict(request.query_params))
    return _hotel_page(result, "act_requests.html", **context)


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
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    if not act_requests.can_handle_act_requests(result):
        return _forbidden_page(tenant_slug)
    stored = await documents_api.read_act_request_file(act_request_id, file_id)
    logger.info(
        "staff.act_request_file_read",
        act_request_id=str(act_request_id),
        file_id=str(file_id),
        user_id=str(result.user_id),
    )
    return file_response(stored)


@router.get(
    "/{tenant_slug}/summary",
    response_class=HTMLResponse,
    summary="Сводка дня: числа за сутки отеля (spec 0035 §7, закрывает #300)",
)
async def summary_page(request: Request, tenant_slug: str) -> Response:
    """Один день отеля числами: создано/закрыто, откуда пришли, медиана взятия,
    просрочено за день, эскалации, открыто сейчас; ниже — разрез по службам.
    Уровень `senior` (числа своих служб, без эскалаций) или `manager`
    (docs/RBAC.md). Своих действий у страницы нет — только чтение, поэтому ни
    JSON-эндпоинта, ни поллинга."""
    del tenant_slug
    result = await _page_context(request, _senior_role)
    if isinstance(result, Response):
        return result
    context = await summary.build_summary_context(
        result, day=summary.parse_summary_day(request.query_params.get("day"))
    )
    return _hotel_page(result, "summary.html", **context)


@router.get(
    "/{tenant_slug}/knowledge",
    response_class=HTMLResponse,
    summary="Справочник отеля: что бот отвечает гостям (spec 0036 §7, #335)",
)
async def knowledge_page(request: Request, tenant_slug: str) -> Response:
    """Факты отеля в порядке хранения, счётчик бюджета, форма добавления и
    вопросы гостей без ответа за 7 дней (spec 0036 §7 п. 3). Роль `manager`
    (docs/RBAC.md); действия — JSON-эндпоинты `api_router.py` под той же
    ролью, после действия `static/knowledge.js` перезагружает страницу."""
    del tenant_slug
    result = await _page_context(request, _manager_role)
    if isinstance(result, Response):
        return result
    context = await knowledge.build_knowledge_context(result)
    return _hotel_page(result, "knowledge.html", **context)


@router.get(
    "/{tenant_slug}/changes",
    response_class=HTMLResponse,
    summary="Журнал изменений: кто что сделал (spec 0041, закрывает #370)",
)
async def changes_page(request: Request, tenant_slug: str) -> Response:
    """Действия сотрудников с заявками и заселением за день отеля; фильтры
    «день / сотрудник / действие» — GET-форма. Уровень `senior` (строки своих
    служб) или `manager` (docs/RBAC.md). Только чтение — ни JSON-эндпоинта, ни
    поллинга (канон сводки дня)."""
    del tenant_slug
    result = await _page_context(request, _senior_role)
    if isinstance(result, Response):
        return result
    context = await changes.build_changes_context(result, dict(request.query_params))
    return _hotel_page(result, "changes.html", **context)


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
    return _hotel_page(result, "team.html", **await team.build_team_context(result))


@router.get(
    "/{tenant_slug}/orders",
    response_class=HTMLResponse,
    summary="Демо «Заказы в номер» к презентации (issue #438)",
    include_in_schema=False,
)
async def demo_orders_page(request: Request, tenant_slug: str) -> Response:
    """Выдуманные заказы ресторана и магазина; кнопки меняют статус только в
    браузере (`static/demo_orders.js`), данных страница не трогает. Выключатель
    `ENABLE_DEMO_ORDERS` выключен — 404, как у неизвестного адреса; включён —
    канон `_page_context`, роль любая."""
    if not get_settings().enable_demo_orders:
        raise HTTPException(status_code=404)
    del tenant_slug
    result = await _page_context(request, _any_role)
    if isinstance(result, Response):
        return result
    context = demo_orders.build_demo_orders_context(result)
    return _hotel_page(result, "demo_orders.html", **context)


@router.get(
    "/{tenant_slug}/handover",
    response_class=HTMLResponse,
    summary="Демо «Передача смены» к собранию в отеле (issue #450)",
    include_in_schema=False,
)
async def demo_handover_page(request: Request, tenant_slug: str) -> Response:
    """Касса к сдаче смены одним кликом на выдуманных данных: «загрузка»
    источников и заполнение бланка идут только в браузере
    (`static/demo_handover.js`), данных страница не трогает. `ENABLE_DEMO_HANDOVER`
    — демо целиком; без него `ENABLE_HANDOVER_MANUAL` — только ручной бланк
    (#469); оба выключены — 404, как у неизвестного адреса. Дальше — канон
    `_page_context`, роль ресепшен или менеджер (касса — их работа)."""
    settings = get_settings()
    if not (settings.enable_demo_handover or settings.enable_handover_manual):
        raise HTTPException(status_code=404)
    del tenant_slug
    result = await _page_context(request, _reception_role)
    if isinstance(result, Response):
        return result
    if settings.enable_demo_handover:
        context = demo_handover.build_demo_handover_context(result)
    else:
        context = demo_handover.build_manual_handover_context(result)
    return _hotel_page(result, "demo_handover.html", **context)


# JSON-действия кабинета (api_router.py) — в конце: служебные литеральные
# маршруты (/login, /logout, /static/*, /invite/*) обязаны регистрироваться
# раньше шаблонных путей с {tenant_slug} (порядок — контракт README пакета).
router.include_router(api_router)
router.include_router(turnover_api_router)
router.include_router(act_request_api_router)
router.include_router(push_api_router)
