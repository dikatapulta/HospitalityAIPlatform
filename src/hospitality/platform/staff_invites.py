"""Приглашения сотрудников (spec 0033 §3.4, spec 0037 §4, ADR-008 §1).

Инвайт — provisioning-артефакт, НЕ способ входа: одноразовая ссылка
`/staff/invite/{token}` (страница — `staff_portal/invites.py`), TTL из
настроек, в БД только SHA-256 токена. Логин сотрудника задаёт менеджер при
выпуске; по принятии ссылка ВСЕГДА создаёт нового User +
`UserIdentity(password)` с этим логином + `TenantMembership` (ревизия ADR-008
27.09.2026: без email узнать «того же человека из другого отеля» не по чему).
Истёкший, отозванный и использованный инвайты неразличимы для держателя
ссылки — один ответ ERR-AUTH-004 («попросите новое приглашение»).

Приглашение без логина (`login IS NULL`, выпущено до spec 0037 или старым
образом при откате) — мёртвое, как истёкшее: принять его нечем.

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
from sqlalchemy import ColumnElement, and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform.models import (
    StaffInvite,
    StaffRole,
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
)
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_INVALID,
    hash_password,
    password_external_id,
    require_login_format,
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
    (в БД — хэш); ссылку из него собирает страница «Сотрудники» (PR F).
    `login` — нормализованный (заглавными), как сотрудник будет входить."""

    invite_id: uuid.UUID
    invite_token: str
    login: str
    expires_at: datetime


class InviteAcceptResult(BaseModel):
    """Итог принятия инвайта: новая учётка, её логин и отель (для автовхода)."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role_key: StaffRole
    login: str


class PendingInviteView(BaseModel):
    """Ожидающая ссылка в списке «Сотрудники» (spec 0033 §7).

    Самого токена здесь нет и быть не может: в БД лежит только хэш, ссылка
    показывается ровно один раз при выпуске. Потерянная ссылка лечится
    отзывом и новым приглашением, а не «показать ещё раз».
    """

    invite_id: uuid.UUID
    invited_name: str
    login: str
    role_key: StaffRole
    expires_at: datetime


class InviteInvitation(BaseModel):
    """Что видит держатель ссылки до ввода пароля: куда, кем и под каким
    логином зовут; `tenant_slug` — код отеля для будущих входов (spec 0037 §4)."""

    tenant_name: str
    tenant_slug: str
    invited_name: str
    login: str
    role_key: StaffRole


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pending(now: datetime) -> ColumnElement[bool]:
    """Ожидающее приглашение: не принято, не истекло/не отозвано и с логином.

    Условие «живого» инвайта для показа, списка и проверки занятости логина:
    строка без логина (до spec 0037 или от старого образа при откате) мертва,
    как истёкшая, — код не полагается на то, что её отозвала миграция
    (spec 0037 §6). `accept_invite` проверяет то же условие в Python ПОСЛЕ
    FOR UPDATE, а не этим выражением в запросе: `now` обязан браться после
    блокировки, иначе отзыв, закоммиченный между вычислением `now` и снимком,
    проскакивает."""
    return and_(
        StaffInvite.accepted_at.is_(None),
        StaffInvite.expires_at > now,
        StaffInvite.login.is_not(None),
    )


async def is_login_taken(session: AsyncSession, tenant_id: uuid.UUID, staff_login: str) -> bool:
    """Занят ли логин в отеле (spec 0037 §4): учёткой — в том числе отключённой,
    её логин не освобождается (§2), — или ожидающим приглашением.

    Единственная проверка занятости: ею пользуются выпуск приглашения и CLI
    бутстрапа. Логин — уже нормализованный (`require_login_format`)."""
    identity = await session.scalar(
        select(UserIdentity.id).where(
            UserIdentity.kind == UserIdentityKind.PASSWORD,
            UserIdentity.external_id == password_external_id(tenant_id, staff_login),
        )
    )
    if identity is not None:
        return True
    invite = await session.scalar(
        select(StaffInvite.id).where(
            StaffInvite.tenant_id == tenant_id,
            StaffInvite.login == staff_login,
            _pending(utc_now()),
        )
    )
    return invite is not None


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
    staff_login: str,
    *,
    invited_by: uuid.UUID,
) -> StaffInviteGrant:
    """Выпустить одноразовую ссылку-приглашение (spec 0033 §3.4, spec 0037 §4).

    Логин проверяется при выпуске: формат и свобода в отеле — среди учёток
    (включая отключённых: их логин остаётся занятым, spec 0037 §2) **и** среди
    ожидающих приглашений. Не тот формат — ERR-AUTH-012 (422), занят — тот же
    код с 409: страница различает их по статусу и пишет свой текст у поля.
    Гонку двух менеджеров с одним логином здесь не закрыть — её ловит UNIQUE
    идентичности при принятии (`accept_invite` → ERR-AUTH-004).

    Повторное приглашение того же человека — новая ссылка; старую менеджер
    гасит `revoke_invite` из списка ожидающих (страница «Сотрудники»
    показывает и то и другое) — до отзыва её логин занят.
    """
    staff_login = require_login_format(staff_login)
    token = secrets.token_urlsafe(32)
    expires_at = utc_now() + timedelta(hours=get_settings().staff_invite_ttl_hours)
    async with platform_session_scope() as session:
        if await is_login_taken(session, tenant_id, staff_login):
            raise AppError(
                code=ERR_AUTH_LOGIN_INVALID,
                message="This login is already taken in this hotel",
                status_code=409,
            )
        invite = StaffInvite(
            tenant_id=tenant_id,
            role_key=role_key,
            invited_name=invited_name,
            login=staff_login,
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
    return StaffInviteGrant(
        invite_id=invite.id, invite_token=token, login=staff_login, expires_at=expires_at
    )


async def list_pending_invites(tenant_id: uuid.UUID) -> list[PendingInviteView]:
    """Ожидающие приглашения тенанта — не принятые и не истёкшие (spec 0033 §7).

    Истёкшие и отозванные (у них `expires_at` в прошлом — одно и то же поле)
    из списка уходят сами, как и приглашения без логина (`_pending`):
    показывать менеджеру мёртвые ссылки незачем, решение по ним всегда одно —
    пригласить заново.
    """
    async with platform_session_scope() as session:
        invites = await session.scalars(
            select(StaffInvite)
            .where(StaffInvite.tenant_id == tenant_id, _pending(utc_now()))
            .order_by(StaffInvite.created_at.desc())
        )
        return [
            PendingInviteView(
                invite_id=invite.id,
                invited_name=invite.invited_name,
                login=invite.login or "",
                role_key=invite.role_key,
                expires_at=invite.expires_at,
            )
            for invite in invites
        ]


async def describe_invite(token: str) -> InviteInvitation | None:
    """Куда и кем зовёт ссылка — для страницы принятия (spec 0033 §3.4).

    Только чтение: невалидный, истёкший, отозванный, уже принятый инвайт и
    инвайт без логина дают одинаковый None (страница показывает «попросите
    новое приглашение»).
    Тенант держателю ссылки не сообщается заранее — он и есть содержимое
    инвайта, а секрет здесь сам токен.
    """
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(StaffInvite, Tenant)
                .join(Tenant, StaffInvite.tenant_id == Tenant.id)
                .where(StaffInvite.token_hash == _hash_token(token), _pending(utc_now()))
            )
        ).one_or_none()
    if row is None:
        return None
    invite, tenant = row
    return InviteInvitation(
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
        invited_name=invite.invited_name,
        login=invite.login or "",
        role_key=invite.role_key,
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


async def accept_invite(token: str, *, password: str) -> InviteAcceptResult:
    """Принять инвайт: пароль → новый User + идентичность с логином + membership.

    Одна транзакция; инвайт берётся FOR UPDATE — конкурентное двойное принятие
    одного токена сериализуется, проигравший видит `accepted_at` и получает
    ERR-AUTH-004 (блокер ревью PR #148: одноразовость — свойство БД, не гонки).

    Принятие ВСЕГДА заводит нового User (spec 0037 §4): чужой пароль здесь не
    проверяется, поэтому ни rate-limit, ни оракула учёток у этой двери нет.
    Логин, занятый между выпуском и принятием (гонка двух менеджеров), ловит
    UNIQUE `(kind, external_id)` — тот же ERR-AUTH-004 «попросите новое
    приглашение»: учётка не создаётся, а инвайт гасится (`expires_at = now`) —
    принять его уже нечем, и в «Ожидают принятия» он не должен висеть живым.
    Вставка идёт в SAVEPOINT, чтобы гашение пережило откат вставки. Длину
    пароля (ERR-AUTH-007) проверяет
    `hash_password` до всякой работы с БД; argon2 — тоже до FOR UPDATE, чтобы
    не держать блокировку инвайта десятки миллисекунд.
    """
    secret_hash = await hash_password(password)
    async with platform_session_scope() as session:
        invite = await session.scalar(
            select(StaffInvite)
            .where(StaffInvite.token_hash == _hash_token(token))
            .with_for_update()
        )
        now = utc_now()
        if (
            invite is None
            or invite.accepted_at is not None
            or invite.expires_at <= now
            or invite.login is None
        ):
            raise _invalid_invite()
        invite_id, tenant_id, role_key = invite.id, invite.tenant_id, invite.role_key
        staff_login = invite.login
        try:
            async with session.begin_nested():
                user = User(display_name=invite.invited_name)
                session.add(user)
                await session.flush()
                session.add(
                    UserIdentity(
                        user_id=user.id,
                        kind=UserIdentityKind.PASSWORD,
                        external_id=password_external_id(tenant_id, staff_login),
                        secret_hash=secret_hash,
                    )
                )
                session.add(
                    TenantMembership(
                        user_id=user.id,
                        tenant_id=tenant_id,
                        role_key=role_key,
                        invited_by=invite.invited_by,
                    )
                )
                await session.flush()
        except IntegrityError:
            await session.execute(
                update(StaffInvite).where(StaffInvite.id == invite_id).values(expires_at=now)
            )
            login_taken = True
        else:
            login_taken = False
            invite.accepted_at = now
            invite.accepted_user_id = user.id
    if login_taken:
        logger.warning(
            "staff.invite_rejected",
            invite_id=str(invite_id),
            tenant_id=str(tenant_id),
            reason="login_taken",
        )
        raise _invalid_invite()
    logger.info(
        "staff.invite_accepted",
        invite_id=str(invite_id),
        user_id=str(user.id),
        tenant_id=str(tenant_id),
        role_key=role_key.value,
    )
    return InviteAcceptResult(
        user_id=user.id,
        tenant_id=tenant_id,
        role_key=role_key,
        login=staff_login,
    )
