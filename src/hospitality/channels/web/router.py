"""HTTP-маршруты канала web (spec 0027 §3.1, ADR-008 §6, §11).

Всё — под `/g/{tenant_slug}/{room_number}` (содержимое комнатного QR), кроме знака
отеля `/g/{tenant_slug}/logo/{file_id}` (spec 0066 §4): он общий для всех комнат. Канал
ставит контекст тенанта САМ внутри маршрутов (slug → tenant_id →
`tenant_context`); в общий `TenantResolver`-middleware ни slug, ни гостевая
сессия не входят — гостевая сессия на `/api/v1/*` конструктивно даёт 401
(тест-инвариант spec 0027 §5 п.12).

Сессия — cookie `guest_session`: HttpOnly + Secure + SameSite=Strict +
Path=/g/{slug} (XSS не читает токен, CSRF отрезан SameSite; тело — только
JSON) и та же cookie на Path=/w/{slug}: повторное сканирование листка с того
же телефона возвращает гостя в его диалог (#223). Cookie-атрибуты — НЕ граница
безопасности: валидность перепроверяет
`guests.resolve_session` на каждом действии (ADR-008 §3). `{room}` из пути на
аутентифицированных операциях игнорируется — комната и Stay берутся только из
сессии (ревью спеки 0027): cookie действует на все комнаты тенанта, а гость,
открывший чужой QR со своей cookie, всё равно действует от своего Stay
(несовпадение логируется).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Final
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Path, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.web import homescreen, look, page, service, wallpaper
from hospitality.channels.web.documents.hotel import guest_documents_enabled
from hospitality.channels.web.schemas import (
    BindSessionResult,
    MessagesPage,
    SendMessageBody,
    SendMessageResult,
    StartSessionBody,
    StartSessionResult,
)
from hospitality.channels.web.table.hotel import table_booking_config
from hospitality.modules.guests import api as guests_api
from hospitality.platform.files import file_response
from hospitality.shared.clientip import client_ip
from hospitality.shared.db import utc_now
from hospitality.shared.errors import ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.middleware import get_correlation_id
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

SESSION_COOKIE = "guest_session"
# Cookie живёт сутки после срока выезда (spec 0060 §2.5): покрывает поздний
# выезд, поставленный уже после привязки, и «+1 ночь».
SESSION_COOKIE_AFTER_CHECKOUT: Final = timedelta(hours=24)

router = APIRouter(prefix="/g", tags=["web-chat"])

# Потолок — у схемы `GuestSessionStart.room_number` (зеркало в сервисе —
# `ROOM_NUMBER_MAX_LENGTH`): длиннее — не номер комнаты, и отказ обязан случиться
# на границе (422), до лимитера и bcrypt, а не ошибкой схемы в домене — как
# `BindToken` ниже (аудит 26.10, К-1 / #483).
RoomNumber = Annotated[str, Path(max_length=service.ROOM_NUMBER_MAX_LENGTH)]


def get_web_llm_provider() -> LlmProvider | None:
    """LLM-провайдер хода по умолчанию (None → боевой провайдер модели `LLM_MODEL`).

    Тесты переопределяют scripted-фейком — тот же приём подмены, что
    `get_orchestrator_provider` телеграм-канала.
    """
    return None


@router.get(wallpaper.PATH.removeprefix(router.prefix), include_in_schema=False)
async def wallpaper_image() -> Response:
    """Обои гостевого чата (#773) — общие для всех отелей, без тенанта и сессии.

    Объявлен раньше страницы чата: тот же путь подошёл бы под `/{tenant_slug}/{room_number}`.
    Кэш навсегда — версию меняет адрес (`wallpaper.URL`), как у статики кабинета.
    """
    return Response(
        wallpaper.IMAGE,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{wallpaper.VERSION}"',
        },
    )


def _icon_route(name: str) -> Callable[[], Awaitable[Response]]:
    async def icon() -> Response:
        return Response(
            homescreen.ICONS[name],
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": homescreen.icon_etag(name),
            },
        )

    return icon


# Иконки чата на экране «Домой» (spec 0068 §2.1) — общие для всех отелей, кэш навсегда,
# как у обоев; маршрут на каждый файл: чужое имя под `/g/static/` — по-прежнему 404 страницы.
for _icon_name in homescreen.ICONS:
    router.add_api_route(
        homescreen.ICON_PATH_PREFIX.removeprefix(router.prefix) + _icon_name,
        _icon_route(_icon_name),
        methods=["GET"],
        include_in_schema=False,
    )


@router.get(
    "/{tenant_slug}/{room_number}",
    response_class=HTMLResponse,
    summary="Страница веб-чата (контент комнатного QR)",
    responses={404: {"model": ErrorResponse, "description": "Неизвестный отель (ERR-WEB-001)"}},
)
async def chat_page(tenant_slug: str, room_number: str) -> HTMLResponse:
    """Статическая страница; slug проверяется, чтобы кривой QR дал 404 сразу.

    Кнопка «Заказать в номер» — только у отеля с включённым room service
    (spec 0038 Р-10), «Забронировать столик» — только с включённой бронью
    (spec 0064 §2.6), скрепка формы реквизитов для АВР — только там, где приём
    включён (spec 0049 Р-7); все видны после входа (JS страницы). Цвет подложки,
    узор и знак — выбор отеля в кабинете (spec 0066 §5), читается каждый раз.
    """
    tenant_id, hotel_name = await service.resolve_hotel(tenant_slug)
    # Комната из адреса — только подсказка: манифест комнаты и поле на экране входа (spec 0068).
    with tenant_context(tenant_id):
        room_service = await service.room_service_config() is not None
        documents = await guest_documents_enabled()
        table_booking = await table_booking_config() is not None
        chat_look = await look.hotel_look()
        dial = await service.reception_room_dial()
    return HTMLResponse(
        page.render(
            hotel_name=hotel_name,
            tenant_slug=tenant_slug,
            room_number=room_number,
            room_service=room_service,
            documents=documents,
            table_booking=table_booking,
            look=chat_look,
            logo_url=look.logo_url(tenant_slug, chat_look),
            reception_room_dial=dial,
        )
    )


@router.get(
    "/{tenant_slug}/{room_number}/manifest.webmanifest",
    summary="Манифест приложения с экрана «Домой» для комнаты (spec 0068)",
    responses={404: {"model": ErrorResponse, "description": "Неизвестный отель (ERR-WEB-001)"}},
)
async def app_manifest(tenant_slug: str, room_number: RoomNumber) -> JSONResponse:
    """Без сессии, как сама страница: под иконкой — название отеля, иконка открывает эту комнату."""
    _tenant_id, hotel_name = await service.resolve_hotel(tenant_slug)
    return JSONResponse(
        homescreen.manifest(
            hotel_name=hotel_name, tenant_slug=tenant_slug, room_number=room_number
        ),
        media_type=homescreen.MEDIA_TYPE,
        # Отель переименовали — новый манифест при следующей установке, без старого кэша.
        headers={"Cache-Control": "no-cache"},
    )


@router.get(
    "/{tenant_slug}/logo/{file_id}",
    summary="Знак отеля в шапке чата и листка (spec 0066 §4)",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Неизвестный отель (ERR-WEB-001) или не знак отеля (ERR-FILES-004)",
        }
    },
)
async def hotel_logo(tenant_slug: str, file_id: uuid.UUID) -> Response:
    """Без сессии: шапка со знаком видна до входа, как название отеля. Отдаётся
    только файл, что сейчас стоит знаком отеля (`look.read_logo`)."""
    tenant_id, _hotel_name = await service.resolve_hotel(tenant_slug)
    with tenant_context(tenant_id):
        stored = await look.read_logo(file_id)
    return file_response(stored)


@router.post(
    "/{tenant_slug}/{room_number}/session",
    summary="Привязка по коду заселения (тройка тенант+комната+код, ADR-008)",
    responses={
        403: {"model": ErrorResponse, "description": "Код не подошёл (ERR-WEB-003)"},
        422: {
            "model": ErrorResponse,
            "description": "Комната длиннее 20 символов или пустая (ERR-PLATFORM-002)",
        },
        429: {
            "model": ErrorResponse,
            "description": "Лимит попыток по IP или по комнате (ERR-WEB-004)",
        },
    },
)
async def start_session(
    tenant_slug: str,
    room_number: RoomNumber,
    body: StartSessionBody,
    request: Request,
    response: Response,
) -> StartSessionResult:
    tenant_id = await service.resolve_tenant(tenant_slug)
    # Настоящий адрес гостя за туннелем — канон `shared/clientip.py` (#207), как
    # у bind-ссылки: без него за туннелем все гости считались бы одним ключом.
    with tenant_context(tenant_id):
        grant = await service.start_session(room_number, body.code, client_ip=client_ip(request))
        session = await service.resolve_session(grant.session_token)
        if session is None:
            # Гонка: Stay погашен (check_out/истечение) между выдачей и чтением —
            # честный auth-only вместо 500 (ревью PR #114).
            raise await service.unauthenticated_error()
    _set_session_cookie(response, tenant_slug, grant.session_token, session.check_out_at)
    return StartSessionResult(room_number=grant.room_number, check_out_at=session.check_out_at)


def _set_session_cookie(
    response: Response, tenant_slug: str, token: str, check_out_at: datetime
) -> None:
    """Cookie гостевой сессии — один контракт для обоих путей привязки (код и
    bind-ссылка): HttpOnly + Secure + SameSite=Strict + Path=/g/{slug};
    Max-Age — до выезда плюс сутки: поздний выезд, поставленный после привязки,
    сдвигает срок, и cookie не должна погаснуть раньше Stay (spec 0060 §2.5).
    Атрибуты — не граница безопасности: её держит `resolve_session` на каждом
    действии, выезд гасит сессию.

    Тот же токен — и на Path=/w/{slug}: без него привязка по листку не видит,
    что браузер уже в чате этого Stay, и каждое сканирование открывало бы гостю
    новый пустой диалог — без итога его заявки (#223)."""
    lifetime = check_out_at + SESSION_COOKIE_AFTER_CHECKOUT - utc_now()
    max_age = max(60, int(lifetime.total_seconds()))
    for path in (f"/g/{tenant_slug}", f"/w/{tenant_slug}"):
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=max_age,
            path=path,
            httponly=True,
            secure=True,
            samesite="strict",
        )


async def require_guest_session(
    tenant_slug: str,
    room_number: str,
    guest_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> tuple[uuid.UUID, guests_api.ActiveGuestSession]:
    """Зависимость аутентифицированных операций: валидная сессия или 401 (Q7/Q8).

    Аналог `require_authenticated_tenant` (§11: эндпоинт рождается
    аутентифицированным), но для актора-гостя: контекст тенанта ставится по
    slug, дальше сессия резолвится УЖЕ внутри контекста (RLS). Любой невалидный
    исход — один и тот же статический 401 без LLM.
    """
    tenant_id = await service.resolve_tenant(tenant_slug)
    with tenant_context(tenant_id):
        session = await service.resolve_session(guest_session)
        if session is None:
            raise await service.unauthenticated_error()
    if room_number.strip() != session.room_number:
        # Гость открыл чат под чужим QR со своей cookie: действия всё равно
        # привязаны к ЕГО Stay (комната — только из сессии).
        logger.info(
            "web_path_room_mismatch", path_room=room_number, session_room=session.room_number
        )
    return tenant_id, session


@router.post(
    "/{tenant_slug}/{room_number}/messages",
    summary="Сообщение гостя → синхронный ответ хода",
    responses={401: {"model": ErrorResponse, "description": "Строгий auth-only (ERR-WEB-002)"}},
)
async def send_message(
    request: Request,
    body: SendMessageBody,
    auth: Annotated[
        tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
    ],
    provider: Annotated[LlmProvider | None, Depends(get_web_llm_provider)],
) -> SendMessageResult:
    tenant_id, session = auth
    correlation_id = get_correlation_id(request) or ""
    with tenant_context(tenant_id):
        return await service.handle_guest_message(
            session,
            body.text,
            body.client_message_id,
            provider=provider,
            correlation_id=correlation_id,
        )


@router.get(
    "/{tenant_slug}/{room_number}/messages",
    summary="История диалога / poll новых сообщений",
    responses={401: {"model": ErrorResponse, "description": "Строгий auth-only (ERR-WEB-002)"}},
)
async def list_messages(
    auth: Annotated[
        tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
    ],
    after: Annotated[uuid.UUID | None, Query()] = None,
) -> MessagesPage:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        messages = await service.list_messages(session, after)
    # Комната сессии — странице: адрес другой комнаты (переселили, старая иконка) она меняет
    # на свой, иначе в шапке стоял бы чужой номер (spec 0068 §2.2).
    return MessagesPage(messages=messages, room_number=session.room_number)


# Ссылка привязки с листка гостю (spec 0033 §6) — короткий префикс /w ради
# ёмкости QR. Отдельный APIRouter: prefix основного — /g. Как и /g, в общий
# `TenantResolver` не входит — контекст тенанта канал ставит сам по slug.
bind_router = APIRouter(prefix="/w", tags=["web-chat"])

# Потолок — у схемы `GuestSessionBind.bind_token`: длиннее — не наш токен, и
# отказ обязан случиться на границе (422), а не ошибкой валидации в сервисе.
BindToken = Annotated[str, Path(max_length=128)]


@bind_router.get(
    "/{tenant_slug}/b/{token}",
    response_class=HTMLResponse,
    summary="Страница ссылки привязки с листка: consent-строка + кнопка",
    responses={404: {"model": ErrorResponse, "description": "Неизвестный отель (ERR-WEB-001)"}},
)
async def bind_page(tenant_slug: str, token: BindToken) -> HTMLResponse:
    """Привязка на GET не случается (открытие страницы — не согласие, spec 0029):
    сессию рождает только POST по нажатию кнопки. Slug проверяется, чтобы кривая
    ссылка дала 404 сразу."""
    tenant_id, hotel_name = await service.resolve_hotel(tenant_slug)
    del token  # проверяет только POST …/session
    with tenant_context(tenant_id):
        chat_look = await look.hotel_look()
        dial = await service.reception_room_dial()
    return HTMLResponse(
        page.render_bind(
            hotel_name=hotel_name,
            look=chat_look,
            logo_url=look.logo_url(tenant_slug, chat_look),
            reception_room_dial=dial,
        )
    )


@bind_router.post(
    "/{tenant_slug}/b/{token}/session",
    summary="Привязка по ссылке с листка: согласие → гостевая сессия",
    responses={
        403: {
            "model": ErrorResponse,
            "description": "Ссылка погашена или проживание закончилось (ERR-GUESTS-006)",
        },
        429: {"model": ErrorResponse, "description": "Лимит привязок по IP (ERR-WEB-005)"},
    },
)
async def bind_session(
    tenant_slug: str,
    token: BindToken,
    request: Request,
    response: Response,
    guest_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> BindSessionResult:
    """Нажатие кнопки-согласия: та же цепочка, что при вводе кода (P-12), —
    привязка → перечитывание сессии → cookie; страница уходит в обычный чат
    `/g/{slug}/{room}` (комната — из привязки, ADR-008 §3). Cookie браузера —
    подсказка «это тот же гость»: сессия того же Stay оставляет его в прежнем
    диалоге (#223), любая другая ничего не меняет."""
    tenant_id = await service.resolve_tenant(tenant_slug)
    # Настоящий адрес гостя за туннелем — канон `shared/clientip.py` (#207):
    # без него все QR-переходы отеля считались бы одним ключом лимита.
    with tenant_context(tenant_id):
        grant = await service.start_session_from_bind_link(
            token, client_ip=client_ip(request), current_session_token=guest_session
        )
        session = await service.resolve_session(grant.session_token)
        if session is None:
            # Гонка: Stay погашен между привязкой и чтением (канон start_session).
            raise await service.unauthenticated_error()
    _set_session_cookie(response, tenant_slug, grant.session_token, session.check_out_at)
    return BindSessionResult(
        room_number=grant.room_number,
        chat_url=f"/g/{tenant_slug}/{quote(grant.room_number, safe='')}",
    )
