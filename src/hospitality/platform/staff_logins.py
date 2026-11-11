"""Входы персонала отеля в кабинет за окно времени — число «пульса пользы» (spec 0035 §8.1).

Вход — новая сессия кабинета (`staff_sessions`): её рождает вход по логину и паролю
(`staff_auth.login`), продление и разблокировка — нет. Сессия принадлежит User,
а не отелю (ADR-008 §1), поэтому отель находится по членству; суперадмин не персонал
отеля и в счёт не идёт, хотя членство в демо-отеле у него может быть.

Таблицы — платформенные, вне RLS (ADR-008 §6): тенант задаёт аргумент, а не контекст.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import distinct, func, select

from hospitality.platform.models import StaffSession, TenantMembership, User
from hospitality.shared.db import platform_session_scope


class StaffLogins(NamedTuple):
    """Входы за окно: сколько раз и сколько разных сотрудников."""

    logins: int
    people: int


async def count_staff_logins(
    tenant_id: uuid.UUID, *, created_after: datetime, created_before: datetime
) -> StaffLogins:
    """Входы сотрудников отеля в полуоткрытом окне `[created_after, created_before)`.

    Границы — от вызывающего (канон `count_escalations`): сутки отеля считает тот, кто
    показывает число. Членство не фильтруется по статусу: отключённый сегодня вошёл
    вчера, и вход был.
    """
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                select(func.count(StaffSession.id), func.count(distinct(StaffSession.user_id)))
                .join(TenantMembership, TenantMembership.user_id == StaffSession.user_id)
                .join(User, User.id == StaffSession.user_id)
                .where(
                    TenantMembership.tenant_id == tenant_id,
                    User.is_platform_admin.is_(False),
                    StaffSession.created_at >= created_after,
                    StaffSession.created_at < created_before,
                )
            )
        ).one()
    return StaffLogins(logins=int(row[0]), people=int(row[1]))
