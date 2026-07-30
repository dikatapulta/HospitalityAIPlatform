"""Приглашения сотрудников (spec 0033 §3.4, ADR-008 §1).

Инвайт — provisioning-артефакт, НЕ способ входа: одноразовая ссылка
`/staff/invite/{token}` (страница — PR F серии), TTL из настроек, в БД только
SHA-256 токена. По принятии создаёт User + `UserIdentity(password)` +
`TenantMembership`; существующий email доказывает владение паролем и получает
только membership (сеть отелей, ADR-008 инвариант а). Истёкший, отозванный и
использованный инвайты неразличимы для держателя ссылки — один ответ
ERR-AUTH-004 («попросите новое приглашение»).

В этом PR серии 0033 модуль никем не вызывается (страница «Сотрудники» — PR F);
поведение приложения не меняется.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select

from hospitality.platform.models import (
    MembershipStatus,
    StaffInvite,
    StaffRole,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_auth import (
    ERR_AUTH_INVALID_CREDENTIALS,
    ERR_AUTH_USER_DEACTIVATED,
    hash_password,
    normalize_email,
    verify_password,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_INVITE_INVALID = "ERR-AUTH-004"


class StaffInviteGrant(BaseModel):
    """Итог создания инвайта. `invite_token` показывается ровно один раз
    (в БД — хэш); ссылку из него собирает страница «Сотрудники» (PR F)."""

    invite_id: uuid.UUID
    invite_token: str
    expires_at: datetime


class InviteAcceptResult(BaseModel):
    """Итог принятия инвайта: кто вошёл в тенанта и создана ли новая личность."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_key: StaffRole
    created_user: bool


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _invalid_invite() -> AppError:
    # Не найден / истёк / отозван / использован — намеренно один ответ:
    # держателю ссылки не сообщается, какая именно судьба у инвайта.
    return AppError(
        code=ERR_AUTH_INVITE_INVALID,
        message="Invite is invalid, expired or already used — ask for a new one",
        status_code=410,
    )


async def create_invite(
    tenant_id: uuid.UUID,
    role_key: StaffRole,
    invited_name: str,
    *,
    invited_by: uuid.UUID,
) -> StaffInviteGrant:
    """Выпустить одноразовую ссылку-приглашение (spec 0033 §3.4).

    Повторное приглашение того же человека — новая ссылка; старую менеджер
    гасит `revoke_invite` из списка ожидающих (PR F показывает и то и другое).
    """
    token = secrets.token_urlsafe(32)
    expires_at = utc_now() + timedelta(hours=get_settings().staff_invite_ttl_hours)
    async with platform_session_scope() as session:
        invite = StaffInvite(
            tenant_id=tenant_id,
            role_key=role_key,
            invited_name=invited_name,
            token_hash=_hash_token(token),
            invited_by=invited_by,
            expires_at=expires_at,
        )
        session.add(invite)
        await session.flush()
    logger.info(
        "staff.invite_created",
        invite_id=str(invite.id),
        tenant_id=str(tenant_id),
        role_key=role_key.value,
        invited_by=str(invited_by),
    )
    return StaffInviteGrant(invite_id=invite.id, invite_token=token, expires_at=expires_at)


async def revoke_invite(invite_id: uuid.UUID, *, actor_user_id: uuid.UUID) -> None:
    """Погасить ожидающий инвайт: `expires_at = now` (отдельного revoked_at нет —
    см. docstring модели). Идемпотентно; уже принятый инвайт гасить нечего —
    ERR-AUTH-004 (membership отзывается деактивацией, не инвайтом)."""
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, invite_id)
        if invite is None or invite.accepted_at is not None:
            raise _invalid_invite()
        now = utc_now()
        if invite.expires_at > now:
            invite.expires_at = now
    logger.info("staff.invite_revoked", invite_id=str(invite_id), actor_user_id=str(actor_user_id))


async def accept_invite(token: str, *, email: str, password: str) -> InviteAcceptResult:
    """Принять инвайт: email+пароль → User (новый или существующий) + membership.

    Одна транзакция. Существующий email обязан доказать владение — пароль
    проверяется против его хэша (иначе держатель ссылки мог бы приписать
    членство чужой учётке): неверный — ERR-AUTH-001, деактивированный —
    ERR-AUTH-005. Существующее членство (повторный инвайт в тот же отель)
    реактивируется и получает роль из инвайта. Имя существующего User не
    перезаписывается (`invited_name` — только для нового).
    """
    email = normalize_email(email)
    async with platform_session_scope() as session:
        invite = await session.scalar(
            select(StaffInvite).where(StaffInvite.token_hash == _hash_token(token))
        )
        now = utc_now()
        if invite is None or invite.accepted_at is not None or invite.expires_at <= now:
            raise _invalid_invite()
        identity = await session.scalar(
            select(UserIdentity).where(
                UserIdentity.kind == UserIdentityKind.PASSWORD,
                UserIdentity.external_id == email,
            )
        )
        created_user = identity is None
        if identity is None:
            user = User(display_name=invite.invited_name)
            session.add(user)
            await session.flush()
            session.add(
                UserIdentity(
                    user_id=user.id,
                    kind=UserIdentityKind.PASSWORD,
                    external_id=email,
                    secret_hash=await hash_password(password),
                )
            )
        else:
            if identity.secret_hash is None or not await verify_password(
                password, identity.secret_hash
            ):
                raise AppError(
                    code=ERR_AUTH_INVALID_CREDENTIALS,
                    message="Invalid email or password",
                    status_code=401,
                )
            existing_user = await session.get(User, identity.user_id)
            assert existing_user is not None  # FK гарантирует
            if existing_user.status is not UserStatus.ACTIVE:
                raise AppError(
                    code=ERR_AUTH_USER_DEACTIVATED,
                    message="User is deactivated",
                    status_code=403,
                )
            user = existing_user
        membership = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.user_id == user.id,
                TenantMembership.tenant_id == invite.tenant_id,
            )
        )
        if membership is None:
            session.add(
                TenantMembership(
                    user_id=user.id,
                    tenant_id=invite.tenant_id,
                    role_key=invite.role_key,
                    invited_by=invite.invited_by,
                )
            )
        else:
            membership.status = MembershipStatus.ACTIVE
            membership.role_key = invite.role_key
            membership.invited_by = invite.invited_by
        invite.accepted_at = now
        invite.accepted_user_id = user.id
    logger.info(
        "staff.invite_accepted",
        invite_id=str(invite.id),
        user_id=str(user.id),
        tenant_id=str(invite.tenant_id),
        role_key=invite.role_key.value,
        created_user=created_user,
    )
    return InviteAcceptResult(
        user_id=user.id,
        tenant_id=invite.tenant_id,
        role_key=invite.role_key,
        created_user=created_user,
    )
