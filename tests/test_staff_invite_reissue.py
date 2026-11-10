"""«Новая ссылка» у приглашения и истёкшие в списке (spec 0037 §4, #944): прежняя
ссылка гаснет, данные приглашения те же; истёкшее видно две недели, отозванное — нет;
логин истёкшего, ушедший другому, — ERR-AUTH-012."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from hospitality.platform.models import StaffInvite, StaffRole, Tenant
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_INVALID
from hospitality.platform.staff_invite_reissue import (
    EXPIRED_INVITE_SHOWN_DAYS,
    list_expired_invites,
    reissue_invite,
)
from hospitality.platform.staff_invites import (
    ERR_AUTH_INVITE_INVALID,
    accept_invite,
    create_invite,
    describe_invite,
    list_pending_invites,
    revoke_invite,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[Tenant, uuid.UUID]:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-r", name="Hotel R")
        session.add(tenant)
        await session.flush()
    manager_id = await create_staff_user(
        unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER
    )
    return tenant, manager_id


async def _expire(invite_id: uuid.UUID, ago: timedelta = timedelta(hours=1)) -> None:
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, invite_id)
        assert invite is not None
        invite.expires_at = utc_now() - ago


async def test_reissue_kills_old_link_and_keeps_invite(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(
        tenant.id,
        StaffRole.SENIOR,
        "Аружан",
        "ARUZ",
        invited_by=manager_id,
        service_keys=["housekeeping"],
    )

    fresh = await reissue_invite(grant.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)

    assert (fresh.invite_id, fresh.login) == (grant.invite_id, "ARUZ")
    assert fresh.invite_token != grant.invite_token
    assert await describe_invite(grant.invite_token) is None
    with pytest.raises(AppError) as old_link:
        await accept_invite(grant.invite_token, password=PASSWORD)
    assert old_link.value.code == ERR_AUTH_INVITE_INVALID
    # Новая ссылка ведёт к тому же приглашению: имя, логин, уровень, службы.
    result = await accept_invite(fresh.invite_token, password=PASSWORD)
    assert (result.login, result.role_key) == ("ARUZ", StaffRole.SENIOR)
    assert await list_pending_invites(tenant.id) == []


async def test_expired_invite_listed_and_reissued(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)
    await _expire(grant.invite_id)

    assert await list_pending_invites(tenant.id) == []
    assert [item.login for item in await list_expired_invites(tenant.id)] == ["DANA"]

    fresh = await reissue_invite(grant.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)

    assert await list_expired_invites(tenant.id) == []
    [pending] = await list_pending_invites(tenant.id)
    assert pending.invite_id == grant.invite_id
    ttl = timedelta(hours=get_settings().staff_invite_ttl_hours)
    assert fresh.expires_at > utc_now() + ttl - timedelta(minutes=1)
    assert await describe_invite(fresh.invite_token) is not None


async def test_old_expired_and_revoked_are_not_listed(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    old = await create_invite(tenant.id, StaffRole.STAFF, "Ерлан", "ERLA", invited_by=manager_id)
    await _expire(old.invite_id, timedelta(days=EXPIRED_INVITE_SHOWN_DAYS, hours=1))
    revoked = await create_invite(tenant.id, StaffRole.STAFF, "Ерик", "ERLB", invited_by=manager_id)
    await revoke_invite(revoked.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    removed = await create_invite(tenant.id, StaffRole.STAFF, "Ерик", "ERLC", invited_by=manager_id)
    await _expire(removed.invite_id)
    # «Убрать» истёкшее — тот же отзыв.
    await revoke_invite(removed.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)

    assert await list_expired_invites(tenant.id) == []
    for gone in (revoked, removed):
        with pytest.raises(AppError) as error:
            await reissue_invite(gone.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
        assert error.value.code == ERR_AUTH_INVITE_INVALID


async def test_reissue_refuses_taken_login_foreign_and_accepted(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel
    expired = await create_invite(tenant.id, StaffRole.STAFF, "Ли", "LIAN", invited_by=manager_id)
    await _expire(expired.invite_id)
    # Логин истёкшего свободен — его успели отдать другому приглашению.
    await create_invite(tenant.id, StaffRole.STAFF, "Ли Ан", "LIAN", invited_by=manager_id)
    with pytest.raises(AppError) as taken:
        await reissue_invite(expired.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    assert (taken.value.code, taken.value.status_code) == (ERR_AUTH_LOGIN_INVALID, 409)

    accepted = await create_invite(tenant.id, StaffRole.STAFF, "Ая", "AYAA", invited_by=manager_id)
    await accept_invite(accepted.invite_token, password=PASSWORD)
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-r2", name="Hotel R2")
        session.add(other)
        await session.flush()
        other_id = other.id
    live = await create_invite(tenant.id, StaffRole.STAFF, "Бек", "BEKK", invited_by=manager_id)
    for invite_id, tenant_id in (
        (accepted.invite_id, tenant.id),
        (live.invite_id, other_id),
        (uuid.uuid4(), tenant.id),
    ):
        with pytest.raises(AppError) as error:
            await reissue_invite(invite_id, tenant_id=tenant_id, actor_user_id=manager_id)
        assert error.value.code == ERR_AUTH_INVITE_INVALID
    # Живая ссылка соседнего отеля не задета.
    assert await describe_invite(live.invite_token) is not None
