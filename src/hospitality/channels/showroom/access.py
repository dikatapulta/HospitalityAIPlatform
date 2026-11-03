"""Допуск на страницу 3D-витрины: slug → отель → режим → cookie (spec 0069 §2.2, ADR-025).

Контекст тенанта канал ставит сам по slug, как веб-чат (ADR-008 §6); в общий
`TenantResolver` slug не входит. Порядок отказов (§4): неизвестный slug, витрина
выключена, сцены нет или режим только кабинетный — 404 `ERR-SHOWROOM-001` раньше
всего остального (один ответ на всё: адрес не подсказывает, есть ли такой отель);
затем — доступ: публичный режим пускает всех, режим по коду — только с cookie,
подписанной текущим кодом (403 `ERR-SHOWROOM-005` без уточнения причины).

Ввод кода — две ступени лимита канона spec 0027 §3.3, обе тратятся каждой попыткой:
по адресу (20 за 10 минут) — первой, чтобы аноним не гонял bcrypt; по отелю (200 за
10 минут) — против подбора с многих адресов. Превышение — 429 `ERR-SHOWROOM-003`;
недоступный Redis — fail-open (канон 0023): код не короче 8 знаков.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from fastapi import Request, Response
from sqlalchemy import select

from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.models import Tenant
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

COOKIE: Final = "showroom_access"
COOKIE_MAX_AGE: Final = 7 * 24 * 3600
CODE_WINDOW_SECONDS: Final = 600
CODE_IP_LIMIT: Final = 20
CODE_TENANT_LIMIT: Final = 200


@dataclass(frozen=True)
class Showroom:
    """Открытая витрина отеля: кто, как называется и её настройки."""

    tenant_id: uuid.UUID
    slug: str
    hotel_name: str
    settings: ShowroomConfig


def _disabled() -> AppError:
    return AppError(
        code=showroom_api.ERR_SHOWROOM_DISABLED,
        message="Showroom is not available",
        status_code=404,
    )


def denied() -> AppError:
    return AppError(
        code=showroom_api.ERR_SHOWROOM_ACCESS_DENIED, message="Access denied", status_code=403
    )


def cookie_path(slug: str) -> str:
    return f"/showroom/{quote(slug, safe='')}"


async def open_showroom(slug: str) -> Showroom:
    """Витрина по slug, если её видно снаружи: включена, есть сцена, есть код или публичный
    режим. Иначе — 404 `001` (кабинетный режим снаружи не виден, §7 п. 3)."""
    async with platform_session_scope() as session:
        row = (
            await session.execute(select(Tenant.id, Tenant.name).where(Tenant.slug == slug))
        ).first()
    if row is None:
        raise _disabled()
    with tenant_context(row.id):
        settings = await showroom_api.load_settings()
        if settings is None or not settings.enabled:
            raise _disabled()
        if not settings.public_enabled and settings.access_code_hash is None:
            raise _disabled()
        if not await showroom_api.has_scene():
            raise _disabled()
    return Showroom(tenant_id=row.id, slug=slug, hotel_name=row.name, settings=settings)


def has_access(showroom: Showroom, request: Request) -> bool:
    """Публичный режим — всем; режим по коду — cookie, подписанная текущим кодом."""
    if showroom.settings.public_enabled:
        return True
    return showroom_api.token_matches(
        showroom.settings, showroom.tenant_id, request.cookies.get(COOKIE)
    )


def require_access(showroom: Showroom, request: Request) -> None:
    if not has_access(showroom, request):
        raise denied()


async def check_code(showroom: Showroom, code: str, *, client_ip: str) -> None:
    """Ввод кода: две ступени лимита, затем bcrypt. Неверный — 403 `005` без причины."""
    await _consume("showroom_code_verify_ip", client_ip, limit=CODE_IP_LIMIT)
    await _consume("showroom_code_verify_tenant", str(showroom.tenant_id), limit=CODE_TENANT_LIMIT)
    with tenant_context(showroom.tenant_id):
        accepted = await showroom_api.verify_access_code(code)
    if not accepted:
        logger.info("showroom_access_rejected", tenant_id=str(showroom.tenant_id))
        raise denied()
    logger.info("showroom_access_granted", tenant_id=str(showroom.tenant_id))


async def _consume(scope: str, key: str, *, limit: int) -> None:
    decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=CODE_WINDOW_SECONDS)
    if decision.available and not decision.allowed:
        # Ключ в лог не пишется: для ступени по адресу это адрес посетителя.
        logger.warning(
            "showroom_rate_limited",
            error_code=showroom_api.ERR_SHOWROOM_RATE_LIMITED,
            scope=scope,
            count=decision.count,
            limit=decision.limit,
        )
        raise AppError(
            code=showroom_api.ERR_SHOWROOM_RATE_LIMITED,
            message="Too many attempts",
            status_code=429,
        )


def set_access_cookie(response: Response, showroom: Showroom) -> None:
    """Cookie доступа на 7 дней: HttpOnly, Secure, SameSite=Strict, только путь витрины."""
    token = showroom_api.access_token(showroom.settings, showroom.tenant_id)
    if token is None:
        return
    response.set_cookie(
        COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        path=cookie_path(showroom.slug),
        httponly=True,
        secure=True,
        samesite="strict",
    )
