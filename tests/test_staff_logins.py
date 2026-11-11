"""Входы персонала отеля за окно — `platform/staff_logins.py` (spec 0035 §8.1, #1050 ч. 2).

Сессия принадлежит User, а не отелю: отель находится по членству. Суперадмин с членством
в отеле и сотрудник соседнего отеля в счёт не идут.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from hospitality.platform.models import (
    StaffRole,
    StaffSession,
    Tenant,
    TenantMembership,
    User,
)
from hospitality.platform.staff_logins import StaffLogins, count_staff_logins
from hospitality.shared.db import platform_session_scope, utc_now


async def _tenant(slug: str) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        return tenant.id


async def _staff(tenant_id: uuid.UUID, *, sessions: int, platform_admin: bool = False) -> uuid.UUID:
    async with platform_session_scope() as session:
        user = User(display_name="Сотрудник", is_platform_admin=platform_admin)
        session.add(user)
        await session.flush()
        session.add(
            TenantMembership(user_id=user.id, tenant_id=tenant_id, role_key=StaffRole.STAFF)
        )
        session.add_all(
            StaffSession(
                user_id=user.id,
                token_hash=uuid.uuid4().hex * 2,
                expires_at=utc_now() + timedelta(days=1),
            )
            for _ in range(sessions)
        )
        return user.id


async def test_counts_logins_and_people_of_the_hotel_only(canonical_database: None) -> None:
    hotel, neighbour = await _tenant("hotel-a"), await _tenant("hotel-b")
    await _staff(hotel, sessions=3)
    await _staff(hotel, sessions=1)
    await _staff(hotel, sessions=0)
    await _staff(hotel, sessions=2, platform_admin=True)
    await _staff(neighbour, sessions=5)
    now = utc_now()

    counted = await count_staff_logins(
        hotel, created_after=now - timedelta(hours=1), created_before=now
    )
    later = await count_staff_logins(
        hotel, created_after=now, created_before=now + timedelta(hours=1)
    )

    assert counted == StaffLogins(logins=4, people=2)
    assert later == StaffLogins(logins=0, people=0)
