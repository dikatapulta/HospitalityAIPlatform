"""Ссылка нового пароля: показ и принятие (spec 0044 §4) — анонимная дверь.

Ссылку выдаёт суперадмин (`staff_superadmin.issue_password_reset`), открывает
сотрудник без сессии — по образцу приглашения (`staff_invites`): токен в пути,
в БД SHA-256, неизвестная, истёкшая, использованная, заменённая ссылка и
ссылка отключённого или стёртого сотрудника неразличимы — один ERR-AUTH-014.

Принятие — одна транзакция: новый argon2-хэш ТОЙ ЖЕ идентичности, все сессии
сотрудника отозваны, ссылка погашена. Логин, членство, роль и история не
меняются. Ссылка FOR UPDATE — два одновременных принятия сериализуются,
второе видит `used_at`. Суперадмину ссылку не выдают (ERR-AUTH-013), а если
строка всё же есть — принятие её не примет: пароль суперадмина задаётся
только командой на сервере.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import select, update

from hospitality.platform.models import (
    StaffPasswordReset,
    StaffSession,
    Tenant,
    User,
    UserIdentity,
    UserStatus,
)
from hospitality.platform.staff_credentials import hash_password, parse_password_external_id
from hospitality.platform.staff_superadmin import hash_token
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_RESET_INVALID = "ERR-AUTH-014"


class PasswordResetInvitation(BaseModel):
    """Что видит держатель ссылки: кто он, в каком отеле и под каким логином."""

    display_name: str
    login: str
    tenant_name: str
    tenant_slug: str


class PasswordResetResult(BaseModel):
    """Итог принятия — для автовхода каноническим `staff_auth.login`."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_slug: str
    login: str


def _invalid_reset() -> AppError:
    return AppError(
        code=ERR_AUTH_RESET_INVALID,
        message="Password reset link is invalid, expired or already used — ask for a new one",
        status_code=410,
    )


async def describe_password_reset(token: str) -> PasswordResetInvitation | None:
    """Кто и куда задаёт пароль — для страницы ссылки; невалидная — None."""
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(StaffPasswordReset, UserIdentity, User)
                .join(UserIdentity, StaffPasswordReset.identity_id == UserIdentity.id)
                .join(User, UserIdentity.user_id == User.id)
                .where(StaffPasswordReset.token_hash == hash_token(token))
            )
        ).one_or_none()
        if row is None:
            return None
        reset, identity, user = row
        parsed = parse_password_external_id(identity.external_id)
        if not _usable(reset, user) or parsed is None:
            return None
        tenant = await session.get(Tenant, parsed[0])
    if tenant is None:
        return None
    return PasswordResetInvitation(
        display_name=user.display_name,
        login=parsed[1],
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
    )


def _usable(reset: StaffPasswordReset, user: User) -> bool:
    """Ссылка жива: не использована, не истекла и не погашена, сотрудник
    активен и не суперадмин. Время — после чтения строки (при принятии — после
    FOR UPDATE, довод `staff_invites._pending`): гашение, закоммиченное до
    снимка, не проскочит."""
    return (
        reset.used_at is None
        and reset.expires_at > utc_now()
        and user.status is UserStatus.ACTIVE
        and not user.is_platform_admin
    )


async def accept_password_reset(token: str, *, password: str) -> PasswordResetResult:
    """Задать новый пароль по ссылке (spec 0044 §4). Длину пароля
    (ERR-AUTH-007) проверяет `hash_password` до всякой работы с БД; argon2 —
    тоже до блокировки ссылки, чтобы не держать её десятки миллисекунд."""
    secret_hash = await hash_password(password)
    async with platform_session_scope() as session:
        reset = await session.scalar(
            select(StaffPasswordReset)
            .where(StaffPasswordReset.token_hash == hash_token(token))
            .with_for_update()
        )
        identity = await session.get(UserIdentity, reset.identity_id) if reset else None
        user = await session.get(User, identity.user_id) if identity else None
        parsed = parse_password_external_id(identity.external_id) if identity else None
        tenant = await session.get(Tenant, parsed[0]) if parsed else None
        if (
            reset is None
            or identity is None
            or user is None
            or parsed is None
            or tenant is None
            or not _usable(reset, user)
        ):
            raise _invalid_reset()
        now = utc_now()
        identity.secret_hash = secret_hash
        reset.used_at = now
        await session.execute(
            update(StaffSession)
            .where(StaffSession.user_id == user.id, StaffSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        result = PasswordResetResult(
            user_id=user.id, tenant_id=tenant.id, tenant_slug=tenant.slug, login=parsed[1]
        )
    logger.info(
        "staff.password_reset_accepted",
        user_id=str(result.user_id),
        tenant_id=str(result.tenant_id),
    )
    return result
