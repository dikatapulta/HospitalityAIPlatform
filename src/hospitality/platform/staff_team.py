"""Состав команды тенанта: список сотрудников, смена уровня и служб, отключение (spec 0033 §7).

Выделено из `staff_auth.py` (R-3, тот же довод, что у `staff_credentials.py`
на ревью PR #153): там — вход и авторизация запроса, здесь — управление
составом, которым пользуется страница «Сотрудники» кабинета.

Все операции ТЕНАНТНО-СКОУПНЫЕ: менеджер отеля видит и меняет только членства
своего тенанта, id сотрудника чужого отеля не даёт ничего (ERR-AUTH-008) —
`require_role` доказывает роль в тенанте, а не право на конкретного человека.
Деактивация при этом остаётся платформенной по замыслу v1 (spec 0033 §3.3:
гасит сессии и ВСЕ членства User разом); здесь у неё появляется тенантная
граница входа — «отключить можно того, кто состоит в моём отеле».

Таблицы — платформенные, вне RLS (ADR-008 §6): единственный доступ к ним —
через этот модуль и `staff_auth`/`staff_invites` (граница R-5).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    StaffSession,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_auth import ERR_AUTH_USER_NOT_FOUND, deactivate_user
from hospitality.platform.staff_credentials import parse_password_external_id
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_SELF_ACTION = "ERR-AUTH-011"
# Учётку суперадмина из кабинета не меняет никто (spec 0044 §2): код здесь, а
# не в `staff_superadmin`, — его проверяют и смена роли, и отключение.
ERR_AUTH_SUPERADMIN_TARGET = "ERR-AUTH-013"


class TenantMemberView(BaseModel):
    """Строка списка «Сотрудники» (spec 0033 §7).

    Статус — две независимые величины: членство в этом отеле
    (`membership_status`) и учётка на платформе (`user_status`). Деактивация
    v1 гасит обе, отзыв членства (RBAC v1) погасит только первую — страница
    показывает их одной подписью, но данные не склеены заранее.
    `login` — логин в ЭТОМ отеле; None — учётка без логина (старая, до spec
    0037, или заведённая старым образом при откате): её отключают и
    приглашают заново. `display_name` и `login` — PII (docs/PII_REGISTRY.md):
    в логи не пишутся. `is_platform_admin` — строка суперадмина (spec 0044
    §2): видна всем, кнопок у неё нет ни у кого.
    """

    user_id: uuid.UUID
    display_name: str
    login: str | None
    is_platform_admin: bool = False
    role_key: StaffRole
    service_keys: list[str] = []
    membership_status: MembershipStatus
    user_status: UserStatus
    last_active_at: datetime | None


def _member_not_found(user_id: uuid.UUID) -> AppError:
    # «Нет такого пользователя» и «он не в моём отеле» — один ответ: id чужого
    # отеля не должен подтверждаться менеджеру соседнего тенанта.
    return AppError(
        code=ERR_AUTH_USER_NOT_FOUND,
        message=f"User {user_id} is not an active member of this hotel",
        status_code=404,
    )


def _forbid_self_action(user_id: uuid.UUID, actor_user_id: uuid.UUID) -> None:
    """Менеджер не меняет роль и не отключает сам себя (ERR-AUTH-011).

    Защита от самоблокировки: снять с себя роль `manager` или отключить свою
    учётку — потерять кабинет отеля без пути назад (реактивации нет,
    ERR-AUTH-005; сброс пароля — только у суперадмина, spec 0044). Свой
    доступ закрывают через другого менеджера или CLI бутстрапа.

    Путей смены роли два, и оба здесь — смена роли и отключение на странице
    «Сотрудники». Третьего больше нет: принятие приглашения всегда заводит
    нового User и чужое членство не трогает (spec 0037 §4).
    """
    if user_id == actor_user_id:
        raise AppError(
            code=ERR_AUTH_SELF_ACTION,
            message="A manager cannot change the role of or deactivate their own account",
            status_code=409,
        )


async def _forbid_superadmin_target(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Учётку суперадмина из кабинета не меняют (ERR-AUTH-013, spec 0044 §2):
    иначе менеджер отеля мог бы снять с оператора роль или отключить его."""
    if await session.scalar(select(User.is_platform_admin).where(User.id == user_id)):
        raise AppError(
            code=ERR_AUTH_SUPERADMIN_TARGET,
            message="The superadmin account is changed only by a server command",
            status_code=409,
        )


async def list_tenant_members(tenant_id: uuid.UUID) -> list[TenantMemberView]:
    """Все членства тенанта — активные и отозванные (spec 0033 §7), кроме
    стёртых суперадмином (spec 0044 §5).

    Отозванные не прячутся: «почему Айгуль не видит заявки» — первый вопрос
    менеджера, и ответ должен быть на той же странице. «Активность» —
    последнее использование живой сессии кабинета (`staff_sessions.
    last_used_at`, обновляется не чаще раза в несколько минут); сессий нет —
    None («не заходил»). Логин — из идентичности этого отеля (spec 0037 §5).
    Порядок: активные выше, внутри — по имени.
    """
    async with platform_session_scope() as session:
        rows = (
            await session.execute(
                select(TenantMembership, User)
                .join(User, TenantMembership.user_id == User.id)
                .where(
                    TenantMembership.tenant_id == tenant_id,
                    User.status != UserStatus.ERASED,
                )
            )
        ).all()
        last_active: dict[uuid.UUID, datetime] = {}
        logins: dict[uuid.UUID, str] = {}
        if rows:
            user_ids = [user.id for _, user in rows]
            for user_id, external_id in await session.execute(
                select(UserIdentity.user_id, UserIdentity.external_id).where(
                    UserIdentity.kind == UserIdentityKind.PASSWORD,
                    UserIdentity.user_id.in_(user_ids),
                )
            ):
                parsed = parse_password_external_id(external_id)
                if parsed is not None and parsed[0] == tenant_id:
                    logins[user_id] = parsed[1]
            last_active = {
                user_id: moment
                for user_id, moment in await session.execute(
                    select(StaffSession.user_id, func.max(StaffSession.last_used_at))
                    .where(
                        StaffSession.user_id.in_(user_ids),
                        StaffSession.revoked_at.is_(None),
                    )
                    .group_by(StaffSession.user_id)
                )
            }
    members = [
        TenantMemberView(
            user_id=user.id,
            display_name=user.display_name,
            login=logins.get(user.id),
            is_platform_admin=user.is_platform_admin,
            role_key=membership.role_key,
            service_keys=list(membership.service_keys),
            membership_status=membership.status,
            user_status=user.status,
            last_active_at=last_active.get(user.id),
        )
        for membership, user in rows
    ]
    members.sort(
        key=lambda member: (
            member.membership_status is not MembershipStatus.ACTIVE,
            member.display_name.lower(),
        )
    )
    return members


async def staffed_service_keys(tenant_id: uuid.UUID) -> frozenset[str]:
    """Службы отеля, у которых есть хотя бы один активный сотрудник (spec 0045 §4).

    Заявку службы без единого такого сотрудника звать некому, и лестница
    поднимает её сразу на третью ступень — к ресепшену и менеджеру. «Активный» —
    живое членство и не отключённая учётка, а не «сейчас на смене»: смену по
    сессиям угадывать эвристикой (вкладка в кармане выглядит «на смене»).
    """
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(TenantMembership.service_keys)
            .join(User, TenantMembership.user_id == User.id)
            .where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
                User.status == UserStatus.ACTIVE,
            )
        )
        return frozenset(key for keys in rows for key in keys)


async def change_member_access(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role_key: StaffRole,
    *,
    service_keys: Sequence[str],
    actor_user_id: uuid.UUID,
) -> None:
    """Сменить уровень и службы сотрудника в ЭТОМ отеле (spec 0033 §7, spec 0045 §6).

    Обе настройки живут на членстве (ADR-008 §1) и меняются вместе, одной
    формой: службы без уровня не имеют смысла (у менеджера их нет). Смена в
    одном отеле не трогает членства в других. `service_keys` уже приведены
    `staff_services.normalize_service_keys` по категориям отеля. Идемпотентна:
    то же самое — не операция. Действует со следующего запроса сотрудника
    (`require_role` читает членство каждый раз) — сессию гасить не нужно.
    """
    _forbid_self_action(user_id, actor_user_id)
    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(
                TenantMembership.user_id == user_id,
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
            )
        )
        if membership is None:
            raise _member_not_found(user_id)
        await _forbid_superadmin_target(session, user_id)
        previous_role, previous_services = membership.role_key, list(membership.service_keys)
        if previous_role is role_key and previous_services == list(service_keys):
            return
        membership.role_key = role_key
        membership.service_keys = list(service_keys)
    logger.info(
        "staff.member_access_changed",
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        previous_role=previous_role.value,
        role_key=role_key.value,
        previous_service_keys=previous_services,
        service_keys=list(service_keys),
        actor_user_id=str(actor_user_id),
    )


async def deactivate_member(
    user_id: uuid.UUID, tenant_id: uuid.UUID, *, actor_user_id: uuid.UUID
) -> None:
    """Отключить сотрудника из кабинета отеля (spec 0033 §3.3/§7).

    Тенантная граница входа: отключить можно только состоящего в этом отеле —
    дальше работает платформенная `staff_auth.deactivate_user` (сессии
    погашены, все членства отозваны, одна транзакция). Проверка членства и
    деактивация — две транзакции: гонка с параллельным отзывом членства
    безобидна (исход тот же), а держать платформенную операцию под чужой
    сессией ради этого не стоит.
    """
    _forbid_self_action(user_id, actor_user_id)
    async with platform_session_scope() as session:
        membership_exists = await session.scalar(
            select(TenantMembership.id).where(
                TenantMembership.user_id == user_id,
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
            )
        )
        if membership_exists is not None:
            await _forbid_superadmin_target(session, user_id)
    if membership_exists is None:
        raise _member_not_found(user_id)
    await deactivate_user(user_id, actor_user_id=actor_user_id)
