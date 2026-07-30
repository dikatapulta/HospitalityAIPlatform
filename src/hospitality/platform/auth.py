"""Аутентификация HTTP: звенья `TenantResolver` и зависимость эндпоинта (FOUNDATION §11).

Phase 0 (Task 0013): один системный клиент — токен (`SERVICE_TOKEN`) привязан
к одному тенанту по slug (`SERVICE_TOKEN_TENANT_SLUG`): клиент не выбирает
себе тенанта (§11), его задаёт конфигурация окружения. Роли и множественные
клиенты — RBAC, Phase 1 (ADR-008, §17.7).

Звенья цепочки `TenantResolver` (ADR-008 §6; собирает composition root через
`chain_resolvers`) и зависимость канонического эндпоинта:

- `resolve_tenant_from_service_token` — API-ключ: валидный токен → tenant_id,
  всё остальное → None (запрос идёт дальше без контекста тенанта).
- `resolve_tenant_from_staff_session` — staff-сессия + slug из пути кабинета
  `/staff/{tenant_slug}/…` (spec 0033 §3.3, PR C серии #48): валидная сессия
  с активным членством → tenant_id, иначе None.
- `require_authenticated_tenant` — FastAPI-зависимость канонического
  эндпоинта: контекст тенанта не установлен → 401 `ERR-PLATFORM-007`.
  Каждый новый роутер обязан включать её (§11: «эндпоинт рождается
  аутентифицированным»); заодно она объявляет bearer-схему в OpenAPI.
"""

from __future__ import annotations

import re
import secrets
import uuid
from typing import Annotated, Final

from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from starlette.requests import cookie_parser
from starlette.types import Scope

from hospitality.platform.models import MembershipStatus, Tenant, TenantMembership
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE, resolve_staff_session
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id_or_none

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_UNAUTHENTICATED = "ERR-PLATFORM-007"

logger = get_logger(module=__name__)

# auto_error=False: 401 отдаёт require_authenticated_tenant каноническим
# конвертом ошибки, а не HTTPException Starlette. Схема нужна и для OpenAPI:
# кнопка Authorize в Swagger UI и поле security у эндпоинтов.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="ServiceToken",
    description="Статический сервисный токен Phase 0 (env SERVICE_TOKEN, FOUNDATION §11)",
)


async def resolve_tenant_from_service_token(scope: Scope) -> uuid.UUID | None:
    """Резолвер тенанта для `TenantContextMiddleware` (контракт `TenantResolver`).

    `Authorization: Bearer <SERVICE_TOKEN>` → id тенанта со slug
    `SERVICE_TOKEN_TENANT_SLUG` из реестра. Любой другой исход — None:
    невалидный токен неотличим от отсутствующего (клиенту не сообщается,
    «почти угадал» ли он). Запросы без заголовка (health, OpenAPI) не
    обращаются к БД вовсе.
    """
    token = _bearer_token(scope)
    if token is None:
        return None
    settings = get_settings()
    # Сравнение за постоянное время: обычное `==` утекает длиной совпавшего
    # префикса (timing attack на подбор токена).
    if not secrets.compare_digest(token.encode(), settings.service_token.encode()):
        return None
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == settings.service_token_tenant_slug)
        )
    if tenant_id is None:
        # Токен верный, но тенант из конфигурации отсутствует в реестре —
        # ошибка окружения (сид не выполнен / опечатка в slug), а не клиента.
        # Клиент получит тот же 401; диагноз — по этому событию в логах.
        logger.warning(
            "service_token_tenant_missing",
            tenant_slug=settings.service_token_tenant_slug,
        )
        return None
    return tenant_id


# Путь кабинета персонала: первый сегмент после /staff/ — slug тенанта
# (spec 0033 §3.3). Служебные сегменты кабинета тенантами не являются и
# резолвером пропускаются без обращения к БД; список — контракт с маршрутами
# `staff_portal/router.py` (login/logout/static — PR C, invite — PR F).
_STAFF_PATH_PATTERN: Final = re.compile(r"^/staff/(?P<slug>[^/]+)")
_STAFF_RESERVED_SEGMENTS: Final = frozenset({"login", "logout", "static", "invite"})


async def resolve_tenant_from_staff_session(scope: Scope) -> uuid.UUID | None:
    """Звено `TenantResolver` кабинета персонала (ADR-008 §6, spec 0033 §3.3).

    Staff-сессия (cookie `STAFF_SESSION_COOKIE`) + slug тенанта из пути
    `/staff/{tenant_slug}/…` → активное членство → tenant_id; любой другой
    исход — None (контракт цепочки: валидная идентичность → tenant_id, иначе
    None, закрыто по умолчанию). Membership проверяется на КАЖДОМ запросе —
    отзыв членства закрывает доступ следующим же запросом.

    Роль здесь не проверяется: это резолюция тенанта для RLS, а не авторизация
    действия — её выполняет `staff_auth.require_role` на самой странице.
    Запросы вне `/staff/…` (в т.ч. `/api/v1/*` со staff-cookie) звено не
    резолвит — staff-сессия не открывает сервисный API.
    """
    match = _STAFF_PATH_PATTERN.match(scope["path"])
    if match is None:
        return None
    slug = match.group("slug")
    if slug in _STAFF_RESERVED_SEGMENTS:
        return None
    token = _staff_session_token(scope)
    if token is None:
        return None
    active = await resolve_staff_session(token)
    if active is None:
        return None
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(TenantMembership.tenant_id)
            .join(Tenant, TenantMembership.tenant_id == Tenant.id)
            .where(
                TenantMembership.user_id == active.user_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
                Tenant.slug == slug,
            )
        )
    return tenant_id


def _staff_session_token(scope: Scope) -> str | None:
    """Значение cookie сессии кабинета из ASGI-scope; нет — None.

    Семантика — ровно Starlette `request.cookies` (`require_role`): тот же
    `cookie_parser`, ВСЕ заголовки Cookie мержатся, последний дубль побеждает.
    Два разных парсера одной сессии расходились бы на кривых соседних cookie
    или нескольких заголовках — авторизация есть, контекста тенанта нет
    (рекомендации ревью PR #153, оба прохода).
    """
    cookies: dict[str, str] = {}
    for name, value in scope["headers"]:
        if name == b"cookie":
            cookies.update(cookie_parser(value.decode("latin-1")))
    return cookies.get(STAFF_SESSION_COOKIE) or None


def _bearer_token(scope: Scope) -> str | None:
    """Достать bearer-токен из ASGI-scope; нет заголовка / не Bearer — None."""
    for name, value in scope["headers"]:
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


async def require_authenticated_tenant(
    # Параметр не читается: токен уже проверил резолвер middleware, зависимость
    # лишь требует результат. Security(...) здесь объявляет bearer-схему в
    # OpenAPI и пробрасывает заголовок в Swagger UI «Authorize».
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)],
) -> uuid.UUID:
    """Зависимость канонического эндпоинта (§11): запрос обязан быть от тенанта.

    Возвращает id текущего тенанта; если middleware не установил контекст
    (нет/неверный токен) — 401 `ERR-PLATFORM-007` с `WWW-Authenticate`.
    Закрыто по умолчанию: даже если резолвер отключат в composition root,
    эндпоинты с этой зависимостью останутся недоступными, а не открытыми.
    """
    tenant_id = current_tenant_id_or_none()
    if tenant_id is None:
        raise AppError(
            code=ERR_UNAUTHENTICATED,
            message="Missing or invalid service token",
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tenant_id
