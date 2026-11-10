"""Кабинет персонала: единственный роутер пакета (spec 0033 §3.3/§4).

Server-rendered внутри монолита (ADR-014): Jinja2-шаблоны + мобильный
CSS-канон, без JS-сборки. Здесь — главная отеля, статика и подключение всего
остального; порядок подключения — контракт README пакета: вход и анонимные
страницы по токену, затем страницы разделов (`*_pages.py`, канон — `pages.py`),
затем JSON-действия (`*_api.py`, канон — `queue_api.py`). Контекст тенанта для
страниц под `/staff/{tenant_slug}/…` ставит звено `TenantResolver`
(`platform/auth.resolve_tenant_from_staff_session`); авторизацию действия
выполняет сама страница (`pages.page_context` поверх `staff_auth.require_role`).
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
- JSON-действия (`*_api.py`: очередь, карточка Stay, состав команды, действия
  разделов) обязаны требовать `Content-Type: application/json` И НЕПУСТОЙ
  same-origin `Origin` (fetch шлёт его всегда): кросс-сайтовая форма не умеет
  JSON-тип, кросс-сайтовый fetch не пройдёт по Origin. Отдельный CSRF-токен не
  нужен, пока действия соблюдают оба правила. Нарушение — 403 `ERR-AUTH-009`.

HTML кабинета аутентифицирован и не кэшируется (`browser.PAGE_HEADERS`:
no-store, запрет фреймов, `Referrer-Policy: strict-origin` — наружу уходит
источник без пути, а `no-referrer` обнулял бы `Origin` у форм и ронял вход,
#164) — страницы живут на личных телефонах за
Cloudflare и несут тексты заявок гостей. CSP — `default-src 'self'`
(рекомендация ревью PR #153): собственный JS страниц — отдельные файлы
`static/*.js`, inline-скрипты и любые чужие источники запрещены.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.staff_portal import (
    act_request_api,
    booking_api,
    catalog_api,
    chat_look_api,
    checkin_pages,
    demo_data_api,
    demo_pages,
    documents_pages,
    floors_api,
    housekeeping_pages,
    ideas_api,
    knowledge_api,
    management_pages,
    mentor_api,
    mentor_assign_api,
    mentor_flag_api,
    mentor_pages,
    order_api,
    pages,
    push_api,
    queue_api,
    requests_pages,
    room_service_pages,
    showroom_api,
    showroom_pages,
    stay_api,
    team_api,
    team_invites_api,
    turnover_api,
)
from hospitality.staff_portal import home as home_page
from hospitality.staff_portal.invites import router as invite_router
from hospitality.staff_portal.login import router as login_router
from hospitality.staff_portal.password_reset import router as password_reset_router
from hospitality.staff_portal.rendering import STATIC_ASSETS
from hospitality.staff_portal.superadmin import router as superadmin_login_router

router = APIRouter(prefix="/staff", tags=["staff-portal"])


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
# внутрь, как вход отеля, без `page_context`.
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
    summary="Главная кабинета отеля: итог смены, карточки с числами, «Требует внимания» (#635)",
)
async def home(request: Request, tenant_slug: str) -> Response:
    """Приветствие по часам отеля, итог смены и заявки, которые ждут дольше
    всех; числа собирает `home.build_home_context`."""
    del tenant_slug  # slug читает require_role из path_params (канон staff_auth)
    result = await pages.page_context(request, pages.any_role)
    if isinstance(result, Response):
        return result
    context = await home_page.build_home_context(result)
    return await pages.hotel_page(result, "home.html", **context)


# Страницы разделов (`readme/`) — по файлу на раздел.
router.include_router(requests_pages.router)
router.include_router(checkin_pages.router)
router.include_router(housekeeping_pages.router)
router.include_router(documents_pages.router)
router.include_router(room_service_pages.router)
router.include_router(mentor_pages.router)
router.include_router(showroom_pages.router)
router.include_router(management_pages.router)
router.include_router(demo_pages.router)

# JSON-действия кабинета — в конце: служебные литеральные маршруты (/login,
# /logout, /static/*, /invite/*) обязаны регистрироваться раньше шаблонных путей
# с {tenant_slug} (порядок — контракт README пакета).
router.include_router(queue_api.router)
router.include_router(stay_api.router)
router.include_router(team_api.router)
router.include_router(knowledge_api.router)
router.include_router(team_invites_api.router)
router.include_router(turnover_api.router)
router.include_router(floors_api.router)
router.include_router(act_request_api.router)
router.include_router(catalog_api.router)
router.include_router(order_api.router)
router.include_router(booking_api.router)
router.include_router(push_api.router)
router.include_router(mentor_api.router)
router.include_router(mentor_assign_api.router)
router.include_router(mentor_flag_api.router)
router.include_router(ideas_api.router)
router.include_router(showroom_api.router)
router.include_router(demo_data_api.router)
router.include_router(chat_look_api.router)
