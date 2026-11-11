"""Хранимый срок ссылок и сессии кабинета — с обеих сторон (#1052).

Остальные тесты смотрят, что ссылка жива сразу и мертва, когда `expires_at`
сдвинули в прошлое; сам срок, записанный при выпуске, не проверял никто — дни
вместо часов проходили зелёными. Образец — срок сессии суперадмина
в `test_staff_superadmin.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from hospitality.platform.models import (
    StaffInvite,
    StaffPasswordReset,
    StaffRole,
    StaffSession,
    Tenant,
)
from hospitality.platform.staff_auth import login
from hospitality.platform.staff_invite_reissue import reissue_invite
from hospitality.platform.staff_invites import create_invite
from hospitality.platform.staff_superadmin import issue_password_reset
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, RECEPTION, create_staff_user, unique_login


@pytest.fixture(autouse=True)
def _counting_rate_limit() -> Iterator[FakeRateLimitRedis]:
    """Свой MonkeyPatch, как в `test_staff_superadmin.py` (довод там)."""
    patcher = pytest.MonkeyPatch()
    fake_redis = FakeRateLimitRedis()
    patcher.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    yield fake_redis
    patcher.undo()


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-t", name="Hotel T")
        session.add(row)
        await session.flush()
        return row


async def _staff(tenant: Tenant, staff_login: str | None = None) -> uuid.UUID:
    return await create_staff_user(
        staff_login or unique_login(),
        tenant_id=tenant.id,
        role=StaffRole.STAFF,
        service_keys=RECEPTION,
    )


async def _manager(tenant: Tenant) -> uuid.UUID:
    return await create_staff_user(unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER)


async def _stored_invite_expiry(invite_id: uuid.UUID) -> datetime:
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, invite_id)
    assert invite is not None
    return invite.expires_at


def _assert_issued_for(expires_at: datetime, ttl: timedelta, before: datetime) -> None:
    """Срок — момент выпуска плюс `ttl`; выпуск — между `before` и этим вызовом."""
    assert before + ttl <= expires_at <= utc_now() + ttl


async def test_invite_link_lives_invite_ttl_hours(tenant: Tenant) -> None:
    manager_id = await _manager(tenant)
    before = utc_now()
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)
    ttl = timedelta(hours=get_settings().staff_invite_ttl_hours)
    _assert_issued_for(await _stored_invite_expiry(grant.invite_id), ttl, before)


async def test_new_link_lives_invite_ttl_hours(tenant: Tenant) -> None:
    manager_id = await _manager(tenant)
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)
    before = utc_now()
    await reissue_invite(grant.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    ttl = timedelta(hours=get_settings().staff_invite_ttl_hours)
    _assert_issued_for(await _stored_invite_expiry(grant.invite_id), ttl, before)


async def test_password_reset_link_lives_reset_ttl_hours(tenant: Tenant) -> None:
    manager_id = await _manager(tenant)
    member_id = await _staff(tenant)
    before = utc_now()
    await issue_password_reset(member_id, tenant.id, actor_user_id=manager_id)
    async with platform_session_scope() as session:
        reset = await session.scalar(select(StaffPasswordReset))
    assert reset is not None
    ttl = timedelta(hours=get_settings().staff_password_reset_ttl_hours)
    _assert_issued_for(reset.expires_at, ttl, before)


async def test_session_lives_absolute_ttl_days(tenant: Tenant) -> None:
    staff_login = unique_login()
    user_id = await _staff(tenant, staff_login)
    before = utc_now()
    await login(tenant.id, staff_login, PASSWORD, client_ip=f"ip-{uuid.uuid4().hex[:8]}")
    async with platform_session_scope() as session:
        staff_session = await session.scalar(
            select(StaffSession).where(StaffSession.user_id == user_id)
        )
    assert staff_session is not None
    ttl = timedelta(days=get_settings().staff_session_absolute_ttl_days)
    _assert_issued_for(staff_session.expires_at, ttl, before)
