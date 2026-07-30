"""HTTP-маршруты канала web (spec 0027 §3.1, ADR-008 §6, §11).

Всё — под `/g/{tenant_slug}/{room_number}` (содержимое комнатного QR). Канал
ставит контекст тенанта САМ внутри маршрутов (slug → tenant_id →
`tenant_context`); в общий `TenantResolver`-middleware ни slug, ни гостевая
сессия не входят — гостевая сессия на `/api/v1/*` конструктивно даёт 401
(тест-инвариант spec 0027 §5 п.12).

Сессия — cookie `guest_session`: HttpOnly + Secure + SameSite=Strict +
Path=/g/{slug} (XSS не читает токен, CSRF отрезан SameSite; тело — только
JSON). Cookie-атрибуты — НЕ граница безопасности: валидность перепроверяет
`guests.resolve_session` на каждом действии (ADR-008 §3). `{room}` из пути на
аутентифицированных операциях игнорируется — комната и Stay берутся только из
сессии (ревью спеки 0027): cookie действует на все комнаты тенанта, а гость,
открывший чужой QR со своей cookie, всё равно действует от своего Stay
(несовпадение логируется).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.web import page, service
from hospitality.channels.web.schemas import (
    BindSessionResult,
    MessagesPage,
    SendMessageBody,
    SendMessageResult,
    StartSessionBody,
    StartSessionResult,
)
from hospitality.modules.guests import api as guests_api
from hospitality.shared.db import utc_now
from hospitality.shared.errors import ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.middleware import get_correlation_id
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

SESSION_COOKIE = "guest_session"

router = APIRouter(prefix="/g", tags=["web-chat"])


def get_web_llm_provider() -> LlmProvider | None:
    """LLM-провайдер хода по умолчанию (None → боевой Anthropic из настроек).

    Тесты переопределяют scripted-фейком — тот же приём подмены, что
    `get_orchestrator_provider` телеграм-канала.
    """
    return None


@router.get(
    "/{tenant_slug}/{room_number}",
    response_class=HTMLResponse,
    summary="Страница веб-чата (контент комнатного QR)",
    responses={404: {"model": ErrorResponse, "description": "Неизвестный отель (ERR-WEB-001)"}},
)
async def chat_page(tenant_slug: str, room_number: str) -> HTMLResponse:
    """Статическая страница; slug проверяется, чтобы кривой QR дал 404 сразу."""
    await service.resolve_tenant(tenant_slug)
    del room_number  # комната — параметр для JS страницы (читает из URL), не сервера
    return HTMLResponse(page.render())


@router.post(
    "/{tenant_slug}/{room_number}/session",
    summary="Привязка по коду заселения (тройка тенант+комната+код, ADR-008)",
    responses={
        403: {"model": ErrorResponse, "description": "Код не подошёл (ERR-WEB-003)"},
        429: {"model": ErrorResponse, "description": "Лимит попыток (ERR-WEB-004)"},
    },
)
async def start_session(
    tenant_slug: str, room_number: str, body: StartSessionBody, response: Response
) -> StartSessionResult:
    tenant_id = await service.resolve_tenant(tenant_slug)
    with tenant_context(tenant_id):
        grant = await service.start_session(room_number.strip(), body.code)
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
    Max-Age — до выезда (атрибуты — не граница безопасности, границу держит
    `resolve_session` на каждом действии)."""
    max_age = max(60, int((check_out_at - utc_now()).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path=f"/g/{tenant_slug}",
        httponly=True,
        secure=True,
        samesite="strict",
    )


async def _require_session(
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
    auth: Annotated[tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(_require_session)],
    provider: Annotated[LlmProvider | None, Depends(get_web_llm_provider)],
) -> SendMessageResult:
    tenant_id, session = auth
    correlation_id = get_correlation_id(request) or ""
    with tenant_context(tenant_id):
        replies, duplicate, last_message_id = await service.handle_guest_message(
            session,
            body.text,
            body.client_message_id,
            provider=provider,
            correlation_id=correlation_id,
        )
    return SendMessageResult(replies=replies, duplicate=duplicate, last_message_id=last_message_id)


@router.get(
    "/{tenant_slug}/{room_number}/messages",
    summary="История диалога / poll новых сообщений",
    responses={401: {"model": ErrorResponse, "description": "Строгий auth-only (ERR-WEB-002)"}},
)
async def list_messages(
    auth: Annotated[tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(_require_session)],
    after: Annotated[uuid.UUID | None, Query()] = None,
) -> MessagesPage:
    tenant_id, session = auth
    with tenant_context(tenant_id):
        messages = await service.list_messages(session, after)
    return MessagesPage(messages=messages)


# Одноразовая QR-ссылка привязки (spec 0033 §6) — короткий префикс /w ради
# ёмкости QR. Отдельный APIRouter: prefix основного — /g. Как и /g, в общий
# `TenantResolver` не входит — контекст тенанта канал ставит сам по slug.
bind_router = APIRouter(prefix="/w", tags=["web-chat"])


@bind_router.get(
    "/{tenant_slug}/b/{token}",
    response_class=HTMLResponse,
    summary="Страница QR-ссылки привязки: consent-строка + кнопка",
    responses={404: {"model": ErrorResponse, "description": "Неизвестный отель (ERR-WEB-001)"}},
)
async def bind_page(tenant_slug: str, token: str) -> HTMLResponse:
    """Токен на GET не потребляется (открытие страницы — не согласие, spec 0029);
    его тратит только POST по нажатию кнопки. Slug проверяется, чтобы кривая
    ссылка дала 404 сразу."""
    await service.resolve_tenant(tenant_slug)
    del token  # потребляет только POST …/session
    return HTMLResponse(page.render_bind())


@bind_router.post(
    "/{tenant_slug}/b/{token}/session",
    summary="Потребление QR-ссылки: согласие → GETDEL → гостевая сессия",
    responses={
        403: {
            "model": ErrorResponse,
            "description": "Ссылка истекла или потреблена (ERR-GUESTS-006)",
        },
        429: {"model": ErrorResponse, "description": "Лимит потребления по IP (ERR-WEB-005)"},
    },
)
async def bind_session(
    tenant_slug: str, token: str, request: Request, response: Response
) -> BindSessionResult:
    """Нажатие кнопки-согласия: та же цепочка, что при вводе кода (P-12), —
    привязка → перечитывание сессии → cookie; страница уходит в обычный чат
    `/g/{slug}/{room}` (комната — из привязки, ADR-008 §3)."""
    tenant_id = await service.resolve_tenant(tenant_slug)
    # За реверс-прокси это адрес прокси, пока uvicorn без --proxy-headers (#149) —
    # тот же компромисс, что у rate-limit логина кабинета.
    client_ip = request.client.host if request.client else "unknown"
    with tenant_context(tenant_id):
        grant = await service.start_session_from_bind_link(token, client_ip=client_ip)
        session = await service.resolve_session(grant.session_token)
        if session is None:
            # Гонка: Stay погашен между привязкой и чтением (канон start_session).
            raise await service.unauthenticated_error()
    _set_session_cookie(response, tenant_slug, grant.session_token, session.check_out_at)
    return BindSessionResult(
        room_number=grant.room_number,
        chat_url=f"/g/{tenant_slug}/{quote(grant.room_number, safe='')}",
    )
