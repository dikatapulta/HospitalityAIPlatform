"""Суперадмин: вход с кодом из приложения, сброс пароля и стирание учёток (spec 0044).

Суперадмин — учётка с `users.is_platform_admin`, логином отеля, членством
`manager` и вторым фактором (`StaffSecondFactor`). Флаг сам доступа к отелю не
даёт (ADR-008 §1, ревизия 24.10.2026): доступ даёт членство, флаг добавляет два
права в этом отеле и требует кода из приложения на входе.

Три двери, все — здесь:

- `superadmin_login` — своя страница входа (spec 0044 §3): логин, пароль и код
  одним ответом ERR-AUTH-001; бюджет попыток общий с обычным входом
  (`staff_credentials`). Обычный `staff_auth.login` суперадмина не пускает.
- `issue_password_reset` — одноразовая ссылка нового пароля (§4); принимает
  её анонимная дверь `staff_password_reset`.
- `erase_member` — стирание учётки (§5).

Каждое действие и каждый вход публикуют `SuperadminActed` В ТОЙ ЖЕ транзакции
(P-6): уведомление основателю не может потеряться отдельно от действия.
Поэтому всё идёт в `session_scope()` отеля — платформенные таблицы вне RLS
читаются и пишутся в ней так же, а outbox требует контекста тенанта.

Права проверяются дважды: зависимость `require_superadmin` (страница) и
повторно в транзакции действия по строке `users` (вызов из другого места не
обойдёт). Логины — PII: в логи не пишутся (правило 2 PII_REGISTRY).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Final

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform import staff_push
from hospitality.platform.events import SuperadminActed, SuperadminActionKind
from hospitality.platform.models import (
    MembershipStatus,
    StaffPasswordReset,
    StaffRole,
    StaffSecondFactor,
    StaffSession,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_auth import (
    ERR_AUTH_FORBIDDEN,
    ERR_AUTH_INVALID_CREDENTIALS,
    ERR_AUTH_USER_NOT_FOUND,
    StaffContext,
    require_role,
)
from hospitality.platform.staff_credentials import (
    TIMING_EQUALIZER_HASH,
    enforce_login_rate_limit,
    parse_password_external_id,
    password_external_id,
    record_failed_login,
    require_login_format,
    verify_password,
)
from hospitality.platform.staff_team import ERR_AUTH_SELF_ACTION, ERR_AUTH_SUPERADMIN_TARGET
from hospitality.platform.totp import matching_step
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_login
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

_manager_role: Final = require_role(StaffRole.MANAGER)


class SuperadminSessionGrant(BaseModel):
    """Итог входа суперадмина; `session_token` показывается один раз (в БД — хэш)."""

    session_token: str
    user_id: uuid.UUID


class PasswordResetGrant(BaseModel):
    """Свежая ссылка нового пароля: токен показывается один раз (в БД — хэш)."""

    reset_token: str
    login: str
    expires_at: datetime


def hash_token(token: str) -> str:
    """SHA-256 токена ссылки — канон `staff_invites` (энтропия 256 бит)."""
    return hashlib.sha256(token.encode()).hexdigest()


def _forbidden() -> AppError:
    return AppError(
        code=ERR_AUTH_FORBIDDEN,
        message="Only the superadmin can do this",
        status_code=403,
    )


async def require_superadmin(request: Request) -> StaffContext:
    """FastAPI-зависимость действий суперадмина: `require_role(manager)` + флаг.

    Не суперадмин — 403 ERR-AUTH-003, тот же ответ, что «не та роль»: кнопок
    он и не видит, а отдельный код подсказал бы, что такие действия есть.
    """
    context = await _manager_role(request)
    if not context.is_platform_admin:
        logger.warning("staff.superadmin_denied", user_id=str(context.user_id))
        raise _forbidden()
    return context


async def superadmin_login(
    tenant_id: uuid.UUID,
    staff_login: str,
    password: str,
    code: str,
    *,
    client_ip: str,
) -> SuperadminSessionGrant:
    """Вход суперадмина: логин отеля + пароль + код из приложения (spec 0044 §3).

    Любой отказ — один ERR-AUTH-001 (нет логина, не суперадмин, неверный
    пароль, нет второго фактора, неверный или повторный код), argon2
    считается всегда. Каждый отказ списывает попытку с бюджета обычного входа.
    Верный пароль с неверным кодом — отказ И событие уведомления: пароль знает
    кто-то без телефона; событие фиксируется до отказа.
    """
    staff_login = require_login_format(staff_login)
    await enforce_login_rate_limit(tenant_id, staff_login, client_ip)
    token = secrets.token_urlsafe(32)
    now = utc_now()
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    reason = "unknown_login"
    with tenant_context(tenant_id):
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(UserIdentity, User)
                    .join(User, UserIdentity.user_id == User.id)
                    .where(
                        UserIdentity.kind == UserIdentityKind.PASSWORD,
                        UserIdentity.external_id == password_external_id(tenant_id, staff_login),
                    )
                )
            ).one_or_none()
            if row is None or row[0].secret_hash is None:
                await verify_password(password, TIMING_EQUALIZER_HASH)
            else:
                identity, user = row
                user_id = user.id
                reason = await _second_step(
                    session, identity, user, password=password, code=code, now=now
                )
                if not reason:
                    ttl = timedelta(minutes=get_settings().staff_superadmin_session_ttl_minutes)
                    staff_session = StaffSession(
                        user_id=user.id, token_hash=hash_token(token), expires_at=now + ttl
                    )
                    session.add(staff_session)
                    await session.flush()
                    session_id = staff_session.id
                    await _announce(session, SuperadminActionKind.LOGIN, user.id, now)
    if user_id is None or session_id is None:
        logger.warning(
            "staff.superadmin_login",
            outcome="rejected",
            reason=reason,
            user_id=str(user_id) if user_id is not None else None,
        )
        await record_failed_login(tenant_id, staff_login, client_ip)
        record_staff_login("rejected")
        raise AppError(
            code=ERR_AUTH_INVALID_CREDENTIALS,
            message="Invalid login, password or code",
            status_code=401,
        )
    logger.info(
        "staff.superadmin_login", outcome="ok", user_id=str(user_id), session_id=str(session_id)
    )
    record_staff_login("ok")
    return SuperadminSessionGrant(session_token=token, user_id=user_id)


async def _second_step(
    session: AsyncSession,
    identity: UserIdentity,
    user: User,
    *,
    password: str,
    code: str,
    now: datetime,
) -> str:
    """Пароль, флаг и код найденной учётки; пустая строка — вход разрешён,
    иначе причина отказа для лога. Принятый код запоминает свой шаг — повтор
    того же кода дальше не подойдёт."""
    if not await verify_password(password, identity.secret_hash or TIMING_EQUALIZER_HASH):
        return "bad_password"
    if not user.is_platform_admin or user.status is not UserStatus.ACTIVE:
        return "not_superadmin"
    factor = await session.get(StaffSecondFactor, user.id, with_for_update=True)
    if factor is None:
        return "no_second_factor"
    step = matching_step(factor.totp_secret, code, now=now, last_used_step=factor.last_used_step)
    if step is None:
        await _announce(session, SuperadminActionKind.LOGIN_BAD_CODE, user.id, now)
        return "bad_code"
    factor.last_used_step = step
    return ""


async def _announce(
    session: AsyncSession,
    kind: SuperadminActionKind,
    actor_user_id: uuid.UUID,
    now: datetime,
    *,
    target_user_id: uuid.UUID | None = None,
    target_role: StaffRole | None = None,
) -> None:
    await publish(
        session,
        SuperadminActed(
            kind=kind,
            occurred_at=now,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            target_role=target_role,
        ),
    )


async def _check_actor(session: AsyncSession, actor_user_id: uuid.UUID) -> None:
    """Повторная проверка в транзакции действия: актор — живой суперадмин."""
    actor = await session.get(User, actor_user_id)
    if actor is None or not actor.is_platform_admin or actor.status is not UserStatus.ACTIVE:
        raise _forbidden()


async def _load_target(
    session: AsyncSession,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[User, TenantMembership]:
    """Цель действия: учётка, у которой есть членство В ЭТОМ отеле (любого
    статуса — отключённых тоже стирают), не стёртая, не своя, не суперадмин.

    FOR UPDATE на строке `users` — сериализация двух суперадмин-действий над
    одной учёткой (стирание против выдачи ссылки)."""
    if user_id == actor_user_id:
        raise AppError(
            code=ERR_AUTH_SELF_ACTION,
            message="The superadmin cannot reset or erase their own account here",
            status_code=409,
        )
    membership = await session.scalar(
        select(TenantMembership).where(
            TenantMembership.user_id == user_id, TenantMembership.tenant_id == tenant_id
        )
    )
    user = await session.get(User, user_id, with_for_update=True)
    if membership is None or user is None or user.status is UserStatus.ERASED:
        raise AppError(
            code=ERR_AUTH_USER_NOT_FOUND,
            message=f"User {user_id} is not a member of this hotel",
            status_code=404,
        )
    if user.is_platform_admin:
        raise AppError(
            code=ERR_AUTH_SUPERADMIN_TARGET,
            message="The superadmin account is changed only by a server command",
            status_code=409,
        )
    return user, membership


async def _hotel_identity(
    session: AsyncSession, user_id: uuid.UUID, tenant_id: uuid.UUID
) -> tuple[UserIdentity, str] | None:
    """Идентичность-пароль учётки В ЭТОМ отеле и её логин; нет — None."""
    for identity in await session.scalars(
        select(UserIdentity).where(
            UserIdentity.user_id == user_id, UserIdentity.kind == UserIdentityKind.PASSWORD
        )
    ):
        parsed = parse_password_external_id(identity.external_id)
        if parsed is not None and parsed[0] == tenant_id:
            return identity, parsed[1]
    return None


async def _expire_live_resets(
    session: AsyncSession, identity_ids: list[uuid.UUID], now: datetime
) -> None:
    """Погасить живые ссылки нового пароля (`expires_at = now`, канон инвайта)."""
    if not identity_ids:
        return
    await session.execute(
        update(StaffPasswordReset)
        .where(
            StaffPasswordReset.identity_id.in_(identity_ids),
            StaffPasswordReset.used_at.is_(None),
            StaffPasswordReset.expires_at > now,
        )
        .values(expires_at=now)
    )


async def issue_password_reset(
    user_id: uuid.UUID, tenant_id: uuid.UUID, *, actor_user_id: uuid.UUID
) -> PasswordResetGrant:
    """Выдать одноразовую ссылку нового пароля (spec 0044 §4).

    Цель — активный сотрудник этого отеля с логином. Новая ссылка гасит
    прежние того же сотрудника. До использования ссылки ничего не меняется:
    старый пароль работает, сессии живы."""
    token = secrets.token_urlsafe(32)
    now = utc_now()
    expires_at = now + timedelta(hours=get_settings().staff_password_reset_ttl_hours)
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await _check_actor(session, actor_user_id)
            user, membership = await _load_target(session, user_id, tenant_id, actor_user_id)
            hotel_identity = await _hotel_identity(session, user_id, tenant_id)
            if (
                user.status is not UserStatus.ACTIVE
                or membership.status is not MembershipStatus.ACTIVE
                or hotel_identity is None
            ):
                raise AppError(
                    code=ERR_AUTH_USER_NOT_FOUND,
                    message=f"User {user_id} has no active login in this hotel",
                    status_code=404,
                )
            identity, staff_login = hotel_identity
            await _expire_live_resets(session, [identity.id], now)
            session.add(
                StaffPasswordReset(
                    identity_id=identity.id,
                    token_hash=hash_token(token),
                    issued_by=actor_user_id,
                    expires_at=expires_at,
                )
            )
            await _announce(
                session,
                SuperadminActionKind.PASSWORD_RESET_ISSUED,
                actor_user_id,
                now,
                target_user_id=user_id,
                target_role=membership.role_key,
            )
    logger.info(
        "staff.password_reset_issued",
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        actor_user_id=str(actor_user_id),
    )
    return PasswordResetGrant(reset_token=token, login=staff_login, expires_at=expires_at)


async def erase_member(
    user_id: uuid.UUID, tenant_id: uuid.UUID, *, actor_user_id: uuid.UUID
) -> None:
    """Стереть учётку (spec 0044 §5): одна транзакция, отключение — в том же шаге.

    `users.status = erased`, сессии и членства отозваны, хэш пароля стёрт,
    ссылки нового пароля погашены, подписки телефонов на уведомления этого
    отеля удалены (spec 0050 §4; в других отелях их удалит рассылка — членство
    там отозвано). Строка идентичности остаётся — логин занят навсегда
    (spec 0037 §2); имя остаётся — история атрибутируема."""
    now = utc_now()
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await _check_actor(session, actor_user_id)
            user, membership = await _load_target(session, user_id, tenant_id, actor_user_id)
            was_active = user.status is UserStatus.ACTIVE
            user.status = UserStatus.ERASED
            await session.execute(
                update(StaffSession)
                .where(StaffSession.user_id == user_id, StaffSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await session.execute(
                update(TenantMembership)
                .where(
                    TenantMembership.user_id == user_id,
                    TenantMembership.status == MembershipStatus.ACTIVE,
                )
                .values(status=MembershipStatus.REVOKED)
            )
            identity_ids = list(
                await session.scalars(
                    select(UserIdentity.id).where(UserIdentity.user_id == user_id)
                )
            )
            await session.execute(
                update(UserIdentity).where(UserIdentity.user_id == user_id).values(secret_hash=None)
            )
            await _expire_live_resets(session, identity_ids, now)
            push_subscriptions = await staff_push.delete_member_subscriptions(session, user_id)
            await _announce(
                session,
                SuperadminActionKind.MEMBER_ERASED,
                actor_user_id,
                now,
                target_user_id=user_id,
                target_role=membership.role_key,
            )
    logger.info(
        "staff.member_erased",
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        actor_user_id=str(actor_user_id),
        was_active=was_active,
        push_subscriptions_deleted=push_subscriptions,
    )
