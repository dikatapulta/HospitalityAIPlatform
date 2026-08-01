"""Приглашения сотрудников (spec 0033 §3.4, ADR-008 §1).

Инвайт — provisioning-артефакт, НЕ способ входа: одноразовая ссылка
`/staff/invite/{token}` (страница — `staff_portal/invites.py`), TTL из
настроек, в БД только SHA-256 токена. По принятии создаёт User +
`UserIdentity(password)` + `TenantMembership`; существующий email доказывает
владение паролем и получает только membership (сеть отелей, ADR-008
инвариант а). Истёкший, отозванный и использованный инвайты неразличимы для
держателя ссылки — один ответ ERR-AUTH-004 («попросите новое приглашение»).

Тенантная граница (PR F): выпуск, показ и отзыв всегда идут с `tenant_id`
менеджера — id инвайта соседнего отеля не даёт ничего. Держателю ссылки
тенант не нужен: его определяет сам токен.
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
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_auth import (
    ERR_AUTH_INVALID_CREDENTIALS,
    ERR_AUTH_USER_DEACTIVATED,
)
from hospitality.platform.staff_credentials import (
    enforce_login_rate_limit,
    ensure_password_policy,
    hash_password,
    normalize_email,
    verify_password,
)
from hospitality.platform.staff_team import ERR_AUTH_SELF_ACTION
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


class PendingInviteView(BaseModel):
    """Ожидающая ссылка в списке «Сотрудники» (spec 0033 §7).

    Самого токена здесь нет и быть не может: в БД лежит только хэш, ссылка
    показывается ровно один раз при выпуске. Потерянная ссылка лечится
    отзывом и новым приглашением, а не «показать ещё раз».
    """

    invite_id: uuid.UUID
    invited_name: str
    role_key: StaffRole
    expires_at: datetime


class InviteInvitation(BaseModel):
    """Что видит держатель ссылки до ввода email и пароля: куда и кем зовут."""

    tenant_name: str
    invited_name: str
    role_key: StaffRole


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
    гасит `revoke_invite` из списка ожидающих (страница «Сотрудники»
    показывает и то и другое).
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


async def list_pending_invites(tenant_id: uuid.UUID) -> list[PendingInviteView]:
    """Ожидающие приглашения тенанта — не принятые и не истёкшие (spec 0033 §7).

    Истёкшие и отозванные (у них `expires_at` в прошлом — одно и то же поле)
    из списка уходят сами: показывать менеджеру мёртвые ссылки незачем,
    решение по ним всегда одно — пригласить заново.
    """
    async with platform_session_scope() as session:
        invites = await session.scalars(
            select(StaffInvite)
            .where(
                StaffInvite.tenant_id == tenant_id,
                StaffInvite.accepted_at.is_(None),
                StaffInvite.expires_at > utc_now(),
            )
            .order_by(StaffInvite.created_at.desc())
        )
        return [
            PendingInviteView(
                invite_id=invite.id,
                invited_name=invite.invited_name,
                role_key=invite.role_key,
                expires_at=invite.expires_at,
            )
            for invite in invites
        ]


async def describe_invite(token: str) -> InviteInvitation | None:
    """Куда и кем зовёт ссылка — для страницы принятия (spec 0033 §3.4).

    Только чтение: невалидный, истёкший, отозванный и уже принятый инвайт
    дают одинаковый None (страница показывает «попросите новое приглашение»).
    Тенант держателю ссылки не сообщается заранее — он и есть содержимое
    инвайта, а секрет здесь сам токен.
    """
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(StaffInvite, Tenant)
                .join(Tenant, StaffInvite.tenant_id == Tenant.id)
                .where(
                    StaffInvite.token_hash == _hash_token(token),
                    StaffInvite.accepted_at.is_(None),
                    StaffInvite.expires_at > utc_now(),
                )
            )
        ).one_or_none()
    if row is None:
        return None
    invite, tenant = row
    return InviteInvitation(
        tenant_name=tenant.name, invited_name=invite.invited_name, role_key=invite.role_key
    )


async def revoke_invite(
    invite_id: uuid.UUID, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID
) -> None:
    """Погасить ожидающий инвайт: `expires_at = now` (отдельного revoked_at нет —
    см. docstring модели). Идемпотентно; уже принятый инвайт гасить нечего —
    ERR-AUTH-004 (membership отзывается деактивацией, не инвайтом).

    `tenant_id` — граница менеджера (PR F): чужой инвайт неотличим от
    несуществующего, тем же ERR-AUTH-004.

    FOR UPDATE — сериализация с конкурентным `accept_invite` (блокер ревью
    PR #148): отзыв не должен «не успеть» между проверкой и принятием."""
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, invite_id, with_for_update=True)
        if invite is None or invite.tenant_id != tenant_id or invite.accepted_at is not None:
            raise _invalid_invite()
        now = utc_now()
        if invite.expires_at > now:
            invite.expires_at = now
    logger.info("staff.invite_revoked", invite_id=str(invite_id), actor_user_id=str(actor_user_id))


def _forbid_manager_downgrade(membership: TenantMembership, role_key: StaffRole) -> None:
    """Принятие инвайта не понижает ДЕЙСТВУЮЩЕГО менеджера отеля (ERR-AUTH-011).

    Третий путь смены роли — рядом с `staff_team.change_member_role` и
    `deactivate_member`, и до ревью PR #159 единственный незакрытый: менеджер,
    открывший собственную ссылку с ролью `staff` и введший свои email и пароль,
    понижал себя молча. Отель оставался без единого менеджера, а обратного пути
    в v1 нет — ни сброса пароля, ни реактивации, только CLI бутстрапа на
    сервере.

    Правило шире «актора» из `_forbid_self_action` намеренно: чья это ссылка —
    своя или коллеги — для последствий неважно, а «понизить менеджера» есть кому
    сделать явным действием на странице «Сотрудники» (там оно и логируется с
    actor). Повышение и подтверждение роли `manager` инвайтом остаются штатными,
    как и любые роли в ДРУГОМ отеле: роль живёт на членстве (ADR-008 §1).
    """
    if (
        membership.status is MembershipStatus.ACTIVE
        and membership.role_key is StaffRole.MANAGER
        and role_key is not StaffRole.MANAGER
    ):
        raise AppError(
            code=ERR_AUTH_SELF_ACTION,
            message="An active manager is not downgraded by accepting an invite",
            status_code=409,
        )


async def accept_invite(
    token: str, *, email: str, password: str, client_ip: str
) -> InviteAcceptResult:
    """Принять инвайт: email+пароль → User (новый или существующий) + membership.

    Одна транзакция; инвайт берётся FOR UPDATE — конкурентное двойное принятие
    одного токена сериализуется, проигравший видит `accepted_at` и получает
    ERR-AUTH-004 (блокер ревью PR #148: одноразовость — свойство БД, не гонки).

    Существующий email обязан доказать владение — пароль проверяется против
    его хэша (иначе держатель ссылки мог бы приписать членство чужой учётке):
    неверный — ERR-AUTH-001, деактивированный — ERR-AUTH-005. Проверка — под
    тем же rate-limit'ом, что login (общий бюджет по email и IP, блокер ревью
    PR #148: иначе инвайт 72 часа служит оракулом подбора пароля и
    перечисления email в обход лимита логина). Неверный пароль инвайт НЕ
    потребляет. Существующее членство (повторный инвайт в тот же отель)
    реактивируется и получает роль из инвайта. Имя существующего User не
    перезаписывается (`invited_name` — только для нового).

    Пароль проверяется на длину ДО поиска личности (блокер ревью PR #159):
    отказ обязан быть одинаковым для занятого и свободного email, иначе
    страница приглашения перечисляет учётки отеля кодом ответа.

    Действующего менеджера этого отеля инвайт НЕ понижает (ERR-AUTH-011): это
    третий путь смены роли, и без него единственный менеджер запирал отель,
    приняв собственную ссылку с ролью `staff` (блокер ревью PR #159).
    """
    email = normalize_email(email)
    ensure_password_policy(password)
    async with platform_session_scope() as session:
        invite = await session.scalar(
            select(StaffInvite)
            .where(StaffInvite.token_hash == _hash_token(token))
            .with_for_update()
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
        if identity is not None:
            await enforce_login_rate_limit(email, client_ip)
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
            _forbid_manager_downgrade(membership, invite.role_key)
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
