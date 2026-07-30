"""Аутентификация персонала: login, сессии, роли, деактивация (spec 0033 §3, ADR-008 §1).

Staff-половина модели идентичности: email+пароль → `StaffSession` (opaque-токен
≥ 256 бит, в БД только SHA-256), активный тенант — атрибут запроса (slug в пути
кабинета), membership и роль проверяются на КАЖДОМ запросе (`require_role`).
В этом PR серии 0033 модуль никем не вызывается — кабинет (`staff_portal/`)
подключит его в PR C, до тех пор поведение приложения не меняется.

Криптографика (канон — guests/service.py, обоснование spec 0033 §3.1/§3.3):
- пароль: argon2id (`argon2-cffi`), в БД только хэш; argon2 блокирует поток
  (~десятки мс) — hash/verify уходят в `asyncio.to_thread`;
- токен сессии: `secrets.token_urlsafe(32)` (256 бит), в БД — SHA-256.

Отказ входа не различим снаружи (нет учётки / неверный пароль — один ответ
ERR-AUTH-001, время выравнено фиктивным verify) — перечисление email запрещено.
Email — PII: в логи не пишется никогда (правило 2 PII_REGISTRY), в ключ
rate-limit уходит его SHA-256.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
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
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_login
from hospitality.shared.ratelimit import consume_rate_limit

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_INVALID_CREDENTIALS = "ERR-AUTH-001"
ERR_AUTH_SESSION_INVALID = "ERR-AUTH-002"
ERR_AUTH_FORBIDDEN = "ERR-AUTH-003"
ERR_AUTH_USER_DEACTIVATED = "ERR-AUTH-005"
ERR_AUTH_LOGIN_RATE_LIMITED = "ERR-AUTH-006"
ERR_AUTH_PASSWORD_TOO_SHORT = "ERR-AUTH-007"
ERR_AUTH_USER_NOT_FOUND = "ERR-AUTH-008"

# Имя cookie сессии кабинета — контракт между этим модулем и staff_portal
# (PR C серии 0033): HttpOnly + Secure + SameSite=Lax ставит слой страниц.
STAFF_SESSION_COOKIE: Final = "staff_session"

# Имя path-параметра тенанта в URL кабинета `/staff/{tenant_slug}/…` (§3.3).
TENANT_SLUG_PATH_PARAM: Final = "tenant_slug"

# Минимальная длина пароля: единственное правило v1 (P-1: без zxcvbn-эвристик;
# фактическая стойкость входа держится argon2 + rate-limit по email и IP).
PASSWORD_MIN_LENGTH: Final = 8

# Параметры argon2id по умолчанию argon2-cffi (RFC 9106 low-memory профиль)
# устраивают v1; смена параметров обратносовместима — verify читает их из хэша.
_password_hasher: Final = PasswordHasher()

# argon2-хэш заведомо несуществующего пароля — выравнивание времени ответа
# (канон _TIMING_EQUALIZER_HASH guests/service.py): отказ «нет такого email»
# не должен отвечать быстрее отказа «пароль не подошёл».
_TIMING_EQUALIZER_HASH: Final = (
    "$argon2id$v=19$m=65536,t=3,p=4$uFVDnGZFgOmMSDz42FpQyQ"
    "$YMNScKQ9hG18S798MuuewCjeBlO2LCiM4kr/AqvxWc8"
)

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
    role_key: StaffRole


def normalize_email(raw: str) -> str:
    """Канон нормализации email-логина (spec 0033 §3.1): trim + lowercase."""
    return raw.strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def hash_password(password: str) -> str:
    """argon2id-хэш пароля; проверяет минимальную длину (ERR-AUTH-007)."""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(
            code=ERR_AUTH_PASSWORD_TOO_SHORT,
            message=f"Password must be at least {PASSWORD_MIN_LENGTH} characters long",
            status_code=422,
        )
    return await asyncio.to_thread(_password_hasher.hash, password)


async def verify_password(password: str, secret_hash: str) -> bool:
    """Проверка пароля против argon2-хэша; любой невалидный исход — False."""
    try:
        return await asyncio.to_thread(_password_hasher.verify, secret_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def enforce_login_rate_limit(email: str, client_ip: str) -> None:
    """Канон 0023, двойной ключ (§3.3): email (подбор пароля к учётке) и IP
    (перебор учёток). Email в ключ Redis уходит хэшем — PII вне Redis.

    Общий бюджет для ВСЕХ дверей, где доказывается пароль: login и принятие
    инвайта существующим email (`staff_invites.accept_invite`, блокер ревью
    PR #148) — троттлинг, обходимый соседней дверью, не троттлинг."""
    settings = get_settings()
    limit = settings.staff_login_rate_limit_attempts
    if limit <= 0:
        return
    keys = (
        ("staff_login_email", hashlib.sha256(email.encode()).hexdigest()),
        ("staff_login_ip", client_ip),
    )
    for scope, key in keys:
        decision = await consume_rate_limit(
            scope,
            key,
            limit=limit,
            window_seconds=settings.staff_login_rate_limit_window_seconds,
        )
        # Fail-open при недоступном Redis — канон 0023 (стойкость держит argon2).
        if decision.available and not decision.allowed:
            logger.warning(
                "staff.login_rate_limited",
                scope=scope,
                count=decision.count,
                limit=decision.limit,
            )
            record_staff_login("rate_limited")
            raise AppError(
                code=ERR_AUTH_LOGIN_RATE_LIMITED,
                message="Too many login attempts — try again later",
                status_code=429,
            )


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
            await verify_password(password, _TIMING_EQUALIZER_HASH)
            logger.warning("staff.login", outcome="rejected", reason="unknown_email")
            raise _rejected_credentials()
        identity, user = row
        if not await verify_password(password, identity.secret_hash):
            logger.warning(
                "staff.login", outcome="rejected", reason="bad_password", user_id=str(user.id)
            )
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
    закрыто по умолчанию — этим же контрактом её подключит TenantResolver (PR C).
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


def require_role(*roles: StaffRole) -> Callable[[Request], Awaitable[StaffContext]]:
    """Канон авторизации страницы/действия кабинета (spec 0033 §3.2, §11 FOUNDATION).

    Фабрика FastAPI-зависимости: `Depends(require_role(StaffRole.MANAGER, ...))`.
    Каждый запрос заново: cookie → сессия (401 ERR-AUTH-002), slug из пути →
    активное членство с ролью из `roles` (403 ERR-AUTH-003; «нет членства» и
    «не та роль» неразличимы — не подсказываем). `is_platform_admin` доступа
    НЕ даёт (ADR-008 §1: действия оператора в данных тенанта — отдельная
    история с явным аудитом, не для v1). Отзыв членства/деактивация закрывают
    доступ следующим же запросом — сессия при этом гаснет только деактивацией.
    """
    allowed = frozenset(roles)
    if not allowed:
        raise ValueError("require_role needs at least one role")

    async def dependency(request: Request) -> StaffContext:
        token = request.cookies.get(STAFF_SESSION_COOKIE)
        active = await resolve_staff_session(token) if token else None
        if active is None:
            raise AppError(
                code=ERR_AUTH_SESSION_INVALID,
                message="Staff session is missing, invalid or expired",
                status_code=401,
            )
        tenant_slug = request.path_params.get(TENANT_SLUG_PATH_PARAM)
        if not isinstance(tenant_slug, str):
            # Ошибка программиста: маршрут кабинета обязан нести {tenant_slug}.
            raise RuntimeError("require_role used on a route without {tenant_slug}")
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
        if row is None or row[0].role_key not in allowed:
            logger.warning(
                "staff.role_denied",
                user_id=str(active.user_id),
                tenant_slug=tenant_slug,
                required=sorted(role.value for role in allowed),
            )
            raise AppError(
                code=ERR_AUTH_FORBIDDEN,
                message="No active membership with a required role for this hotel",
                status_code=403,
            )
        membership, tenant = row
        return StaffContext(
            session_id=active.session_id,
            user_id=active.user_id,
            display_name=active.display_name,
            is_platform_admin=active.is_platform_admin,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            role_key=membership.role_key,
        )

    return dependency


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
