"""Аутентификация персонала: login, сессии, роли, деактивация (spec 0033 §3, ADR-008 §1).

Staff-половина модели идентичности: email+пароль → `StaffSession` (opaque-токен
≥ 256 бит, в БД только SHA-256), активный тенант — атрибут запроса (slug в пути
кабинета), membership и роль проверяются на КАЖДОМ запросе (`require_role`).
Потребители — страницы кабинета (`staff_portal/`) и звено `TenantResolver`
кабинета (`platform/auth.py`). Пароли, нормализация email и rate-limit входа —
в `staff_credentials.py` (R-3, ревью PR #153).

Отказ входа не различим снаружи (нет учётки / неверный пароль — один ответ
ERR-AUTH-001, время выравнено фиктивным verify) — перечисление email запрещено.
Email — PII: в логи не пишется никогда (правило 2 PII_REGISTRY).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    StaffSession,
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_credentials import (
    TIMING_EQUALIZER_HASH,
    enforce_login_rate_limit,
    normalize_email,
    record_failed_login,
    verify_password,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_login
from hospitality.shared.tenancy import current_tenant_id_or_none

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_INVALID_CREDENTIALS = "ERR-AUTH-001"
ERR_AUTH_SESSION_INVALID = "ERR-AUTH-002"
ERR_AUTH_FORBIDDEN = "ERR-AUTH-003"
ERR_AUTH_USER_DEACTIVATED = "ERR-AUTH-005"
ERR_AUTH_USER_NOT_FOUND = "ERR-AUTH-008"

# Имя cookie сессии кабинета — контракт между этим модулем и staff_portal
# (PR C серии 0033): HttpOnly + Secure + SameSite=Lax ставит слой страниц.
STAFF_SESSION_COOKIE: Final = "staff_session"

# Имя path-параметра тенанта в URL кабинета `/staff/{tenant_slug}/…` (§3.3).
TENANT_SLUG_PATH_PARAM: Final = "tenant_slug"

# Ключ ASGI `scope["state"]`, под которым звено TenantResolver кабинета
# (`platform/auth.py`) кладёт готовый StaffContext запроса: резолвер уже
# проверил сессию и членство — `require_role` не повторяет те же 2 запроса
# (дедуп 4 платформенных запросов на запрос, рекомендация ревью PR #153).
# Состояние живёт ровно один запрос — Starlette создаёт scope["state"] заново.
STAFF_CONTEXT_STATE_KEY: Final = "staff_context"

# last_used_at обновляется не чаще раза в этот интервал — канон
# GuestSession.last_used_at (не писать в БД на каждый poll очереди заявок).
_LAST_USED_REFRESH_SECONDS: Final = 300


class StaffMembership(BaseModel):
    """Активное членство сотрудника: куда можно войти и кем (ADR-008 §1)."""

    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_name: str
    role_key: StaffRole


class StaffSessionGrant(BaseModel):
    """Итог успешного логина. `session_token` показывается ровно один раз:
    в БД только хэш; membership'ы — для редиректа после входа (§3.3: одно
    членство → сразу в тенанта, несколько — экран выбора; PR C)."""

    session_token: str
    user_id: uuid.UUID
    display_name: str
    memberships: list[StaffMembership]


class ActiveStaffUser(BaseModel):
    """Валидная сессия кабинета — личность БЕЗ тенанта (сессия принадлежит
    User; тенант — атрибут запроса, проверяется отдельно в `require_role`)."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    is_platform_admin: bool


class StaffContext(BaseModel):
    """Актор запроса кабинета: кто действует, в каком тенанте и какой ролью.

    Возвращается `require_role` — страницы берут отсюда actor для логов
    (`user_id` в каждом действии кабинета, spec 0033 §9) и `acting_user`
    доменных операций (PR D).
    """

    session_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    is_platform_admin: bool
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_name: str
    role_key: StaffRole


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _rejected_credentials() -> AppError:
    record_staff_login("rejected")
    return AppError(
        code=ERR_AUTH_INVALID_CREDENTIALS,
        message="Invalid email or password",
        status_code=401,
    )


async def login(email: str, password: str, *, client_ip: str) -> StaffSessionGrant:
    """Вход по email+паролю → новая сессия кабинета (spec 0033 §3.3).

    Отказы: ERR-AUTH-001 (нет учётки/неверный пароль — неразличимы, время
    выравнено), ERR-AUTH-005 (деактивирован), ERR-AUTH-006 (rate-limit).
    Каждый логин — новая сессия (второе устройство — норма); отзыв разом —
    деактивация.

    Бюджет попыток тратят только отказы ERR-AUTH-001 (`record_failed_login`,
    issue #207): успешный вход и отказ деактивированному пароль доказали.
    """
    email = normalize_email(email)
    await enforce_login_rate_limit(email, client_ip)
    token = secrets.token_urlsafe(32)
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(UserIdentity, User)
                .join(User, UserIdentity.user_id == User.id)
                .where(
                    UserIdentity.kind == UserIdentityKind.PASSWORD,
                    UserIdentity.external_id == email,
                )
            )
        ).one_or_none()
        if row is None or row[0].secret_hash is None:
            await verify_password(password, TIMING_EQUALIZER_HASH)
            logger.warning("staff.login", outcome="rejected", reason="unknown_email")
            await record_failed_login(email, client_ip)
            raise _rejected_credentials()
        identity, user = row
        if not await verify_password(password, identity.secret_hash):
            logger.warning(
                "staff.login", outcome="rejected", reason="bad_password", user_id=str(user.id)
            )
            await record_failed_login(email, client_ip)
            raise _rejected_credentials()
        if user.status is not UserStatus.ACTIVE:
            logger.warning(
                "staff.login", outcome="rejected", reason="deactivated", user_id=str(user.id)
            )
            record_staff_login("rejected")
            raise AppError(
                code=ERR_AUTH_USER_DEACTIVATED,
                message="User is deactivated",
                status_code=403,
            )
        staff_session = StaffSession(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=utc_now() + timedelta(days=get_settings().staff_session_absolute_ttl_days),
        )
        session.add(staff_session)
        await session.flush()
        memberships = await _load_active_memberships(session, user.id)
    logger.info(
        "staff.login",
        outcome="ok",
        user_id=str(user.id),
        session_id=str(staff_session.id),
        memberships=len(memberships),
    )
    record_staff_login("ok")
    return StaffSessionGrant(
        session_token=token,
        user_id=user.id,
        display_name=user.display_name,
        memberships=memberships,
    )


async def resolve_staff_session(token: str) -> ActiveStaffUser | None:
    """Валидность сессии НА КАЖДОМ запросе; любой невалидный исход — None.

    Проверяется всё разом (канон guests.resolve_session): хэш найден, не
    отозвана, absolute-срок (`expires_at`) не вышел, idle-срок по
    `last_used_at` не вышел, User активен. Активность продлевает idle-срок
    (запись — не чаще раза в `_LAST_USED_REFRESH_SECONDS`).
    Контракт резолвера (ADR-008 §6): валидная идентичность или None,
    закрыто по умолчанию — этим же контрактом её подключает TenantResolver.
    """
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(StaffSession, User)
                .join(User, StaffSession.user_id == User.id)
                .where(StaffSession.token_hash == _hash_token(token))
            )
        ).one_or_none()
        if row is None:
            return None
        staff_session, user = row
        now = utc_now()
        idle_limit = timedelta(days=get_settings().staff_session_idle_ttl_days)
        if (
            staff_session.revoked_at is not None
            or now >= staff_session.expires_at
            or now - staff_session.last_used_at > idle_limit
            or user.status is not UserStatus.ACTIVE
        ):
            return None
        if (now - staff_session.last_used_at).total_seconds() > _LAST_USED_REFRESH_SECONDS:
            staff_session.last_used_at = now
        return ActiveStaffUser(
            session_id=staff_session.id,
            user_id=user.id,
            display_name=user.display_name,
            is_platform_admin=user.is_platform_admin,
        )


async def logout(token: str) -> None:
    """Погасить сессию по токену; идемпотентно (повторный logout — no-op)."""
    async with platform_session_scope() as session:
        staff_session = await session.scalar(
            select(StaffSession).where(
                StaffSession.token_hash == _hash_token(token),
                StaffSession.revoked_at.is_(None),
            )
        )
        if staff_session is None:
            return
        staff_session.revoked_at = utc_now()
    logger.info(
        "staff.logout", user_id=str(staff_session.user_id), session_id=str(staff_session.id)
    )


async def deactivate_user(user_id: uuid.UUID, *, actor_user_id: uuid.UUID) -> None:
    """Деактивация — одно действие, одна транзакция (spec 0033 §3.3, DoD #48):
    `users.status=deactivated`, все сессии revoked, все членства revoked.

    Действует на ВСЮ платформу, не на один тенант (v1: у пилота один отель;
    пер-тенантный отзыв — это revoke membership, появится с RBAC v1, если
    попросит сеть отелей). Аудит — лог-событие с actor; таблица аудита — RBAC v1.
    """
    async with platform_session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError(
                code=ERR_AUTH_USER_NOT_FOUND,
                message=f"User {user_id} does not exist",
                status_code=404,
            )
        now = utc_now()
        user.status = UserStatus.DEACTIVATED
        sessions_revoked = 0
        for staff_session in await session.scalars(
            select(StaffSession).where(
                StaffSession.user_id == user_id, StaffSession.revoked_at.is_(None)
            )
        ):
            staff_session.revoked_at = now
            sessions_revoked += 1
        for membership in await session.scalars(
            select(TenantMembership).where(
                TenantMembership.user_id == user_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
            )
        ):
            membership.status = MembershipStatus.REVOKED
    logger.info(
        "staff.user_deactivated",
        user_id=str(user_id),
        actor_user_id=str(actor_user_id),
        sessions_revoked=sessions_revoked,
    )


async def load_staff_context(active: ActiveStaffUser, tenant_slug: str) -> StaffContext | None:
    """Активное членство пользователя в тенанте по slug → полный `StaffContext`.

    Единственный запрос «membership + tenant» кабинета: его зовут звено
    `TenantResolver` (`platform/auth.py`, кладёт результат в scope state) и
    фолбэк `require_role` (вызовы вне HTTP-конвейера — юниты, скрипты).
    Нет активного членства или slug не существует — None (неразличимы).
    """
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(TenantMembership, Tenant)
                .join(Tenant, TenantMembership.tenant_id == Tenant.id)
                .where(
                    TenantMembership.user_id == active.user_id,
                    TenantMembership.status == MembershipStatus.ACTIVE,
                    Tenant.slug == tenant_slug,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    membership, tenant = row
    return StaffContext(
        session_id=active.session_id,
        user_id=active.user_id,
        display_name=active.display_name,
        is_platform_admin=active.is_platform_admin,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tenant_name=tenant.name,
        role_key=membership.role_key,
    )


def _stashed_staff_context(request: Request, tenant_slug: str) -> StaffContext | None:
    """StaffContext из scope state, если его положило звено резолвера ЭТОГО
    запроса и slug совпадает (страница и резолвер видят один и тот же путь)."""
    state = request.scope.get("state")
    context = state.get(STAFF_CONTEXT_STATE_KEY) if isinstance(state, dict) else None
    if isinstance(context, StaffContext) and context.tenant_slug == tenant_slug:
        return context
    return None


def require_role(*roles: StaffRole) -> Callable[[Request], Awaitable[StaffContext]]:
    """Канон авторизации страницы/действия кабинета (spec 0033 §3.2, §11 FOUNDATION).

    Фабрика FastAPI-зависимости: `Depends(require_role(StaffRole.MANAGER, ...))`.
    Каждый запрос заново: cookie → сессия (401 ERR-AUTH-002), slug из пути →
    активное членство с ролью из `roles` (403 ERR-AUTH-003; «нет членства» и
    «не та роль» неразличимы — не подсказываем). Готовый StaffContext запроса
    берётся из scope state (его положило звено TenantResolver — те же проверки
    уже пройдены); фолбэк с полными запросами — для вызовов вне HTTP-конвейера.

    После авторизации — сверка с RLS-контекстом запроса (fail-closed 403,
    ревью PR #153): контекст ставит независимое звено цепочки резолверов, и
    при экзотическом запросе «SERVICE_TOKEN тенанта A + staff-cookie с
    членством в B» действие исполнилось бы под чужим контекстом БД. Проверка
    живёт здесь, а не в страницах: JSON-действия (PR D) — под той же защитой.

    `is_platform_admin` доступа НЕ даёт (ADR-008 §1: действия оператора в
    данных тенанта — отдельная история с явным аудитом, не для v1). Отзыв
    членства/деактивация закрывают доступ следующим же запросом — сессия при
    этом гаснет только деактивацией.
    """
    allowed = frozenset(roles)
    if not allowed:
        raise ValueError("require_role needs at least one role")

    async def dependency(request: Request) -> StaffContext:
        tenant_slug = request.path_params.get(TENANT_SLUG_PATH_PARAM)
        if not isinstance(tenant_slug, str):
            # Ошибка программиста: маршрут кабинета обязан нести {tenant_slug}.
            raise RuntimeError("require_role used on a route without {tenant_slug}")
        context = _stashed_staff_context(request, tenant_slug)
        if context is None:
            token = request.cookies.get(STAFF_SESSION_COOKIE)
            active = await resolve_staff_session(token) if token else None
            if active is None:
                raise AppError(
                    code=ERR_AUTH_SESSION_INVALID,
                    message="Staff session is missing, invalid or expired",
                    status_code=401,
                )
            context = await load_staff_context(active, tenant_slug)
        if context is None or context.role_key not in allowed:
            logger.warning(
                "staff.role_denied",
                user_id=str(context.user_id) if context else None,
                tenant_slug=tenant_slug,
                required=sorted(role.value for role in allowed),
            )
            raise AppError(
                code=ERR_AUTH_FORBIDDEN,
                message="No active membership with a required role for this hotel",
                status_code=403,
            )
        if current_tenant_id_or_none() != context.tenant_id:
            logger.warning(
                "staff.tenant_context_mismatch",
                authorized_tenant_id=str(context.tenant_id),
                context_tenant_id=str(current_tenant_id_or_none() or ""),
                user_id=str(context.user_id),
            )
            raise AppError(
                code=ERR_AUTH_FORBIDDEN,
                message="Request tenant context does not match the authorized hotel",
                status_code=403,
            )
        return context

    return dependency


async def list_memberships(user_id: uuid.UUID) -> list[StaffMembership]:
    """Активные членства пользователя — экран выбора отеля (spec 0033 §3.3, PR C).

    Логин возвращает membership'ы в `StaffSessionGrant`; этот путь — для
    повторных заходов на `/staff/` по живой cookie, когда логина не было.
    """
    async with platform_session_scope() as session:
        return await _load_active_memberships(session, user_id)


async def _load_active_memberships(
    session: AsyncSession, user_id: uuid.UUID
) -> list[StaffMembership]:
    rows = (
        await session.execute(
            select(TenantMembership, Tenant)
            .join(Tenant, TenantMembership.tenant_id == Tenant.id)
            .where(
                TenantMembership.user_id == user_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
            )
            .order_by(Tenant.slug)
        )
    ).all()
    return [
        StaffMembership(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            tenant_name=tenant.name,
            role_key=membership.role_key,
        )
        for membership, tenant in rows
    ]
