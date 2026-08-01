"""Состав команды тенанта: список сотрудников, смена роли, отключение (spec 0033 §7).

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
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import func, select

from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    StaffSession,
    TenantMembership,
    User,
    UserStatus,
)
from hospitality.platform.staff_auth import ERR_AUTH_USER_NOT_FOUND, deactivate_user
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_SELF_ACTION = "ERR-AUTH-011"


class TenantMemberView(BaseModel):
    """Строка списка «Сотрудники» (spec 0033 §7).

    Статус — две независимые величины: членство в этом отеле
    (`membership_status`) и учётка на платформе (`user_status`). Деактивация
    v1 гасит обе, отзыв членства (RBAC v1) погасит только первую — страница
    показывает их одной подписью, но данные не склеены заранее.
    `display_name` — PII (docs/PII_REGISTRY.md): в логи не пишется.
    """

    user_id: uuid.UUID
    display_name: str
    role_key: StaffRole
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
    учётку — потерять кабинет отеля без пути назад (сброса пароля и
    реактивации в v1 нет, ERR-AUTH-001/-005). Свой доступ закрывают через
    другого менеджера или CLI бутстрапа.

    Здесь закрыты два пути смены роли из трёх; третий — принятие приглашения
    (`staff_invites._forbid_manager_downgrade`, тот же код, ревью PR #159).
    Правило меняется целиком: правишь одно место — правишь и второе (P-12).
    """
    if user_id == actor_user_id:
        raise AppError(
            code=ERR_AUTH_SELF_ACTION,
            message="A manager cannot change the role of or deactivate their own account",
            status_code=409,
        )


async def list_tenant_members(tenant_id: uuid.UUID) -> list[TenantMemberView]:
    """Все членства тенанта — активные и отозванные (spec 0033 §7).

    Отозванные не прячутся: «почему Айгуль не видит заявки» — первый вопрос
    менеджера, и ответ должен быть на той же странице. «Активность» —
    последнее использование живой сессии кабинета (`staff_sessions.
    last_used_at`, обновляется не чаще раза в несколько минут); сессий нет —
    None («не заходил»). Порядок: активные выше, внутри — по имени.
    """
    async with platform_session_scope() as session:
        rows = (
            await session.execute(
                select(TenantMembership, User)
                .join(User, TenantMembership.user_id == User.id)
                .where(TenantMembership.tenant_id == tenant_id)
            )
        ).all()
        last_active: dict[uuid.UUID, datetime] = {}
        if rows:
            last_active = {
                user_id: moment
                for user_id, moment in await session.execute(
                    select(StaffSession.user_id, func.max(StaffSession.last_used_at))
                    .where(
                        StaffSession.user_id.in_([user.id for _, user in rows]),
                        StaffSession.revoked_at.is_(None),
                    )
                    .group_by(StaffSession.user_id)
                )
            }
    members = [
        TenantMemberView(
            user_id=user.id,
            display_name=user.display_name,
            role_key=membership.role_key,
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


async def change_member_role(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role_key: StaffRole,
    *,
    actor_user_id: uuid.UUID,
) -> None:
    """Сменить роль сотрудника в ЭТОМ отеле (spec 0033 §7).

    Роль живёт на членстве (ADR-008 §1), поэтому смена роли в одном отеле не
    трогает членства в других. Идемпотентна: та же роль — не операция.
    Действует со следующего запроса сотрудника (`require_role` проверяет
    членство каждый раз) — сессию гасить не нужно.
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
        previous = membership.role_key
        if previous is role_key:
            return
        membership.role_key = role_key
    logger.info(
        "staff.member_role_changed",
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        previous_role=previous.value,
        role_key=role_key.value,
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
    if membership_exists is None:
        raise _member_not_found(user_id)
    await deactivate_user(user_id, actor_user_id=actor_user_id)
