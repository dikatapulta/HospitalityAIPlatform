"""Приглашения сотрудников (spec 0033 §3.4, §10): одноразовость, истечение,
отзыв, существующий email → второе членство (сеть отелей, ADR-008 инвариант а).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.platform.models import (
    MembershipStatus,
    StaffInvite,
    StaffRole,
    Tenant,
    TenantMembership,
    User,
)
from hospitality.platform.staff_auth import (
    ERR_AUTH_INVALID_CREDENTIALS,
    ERR_AUTH_PASSWORD_TOO_SHORT,
    login,
)
from hospitality.platform.staff_invites import (
    ERR_AUTH_INVITE_INVALID,
    accept_invite,
    create_invite,
    revoke_invite,
)
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from tests.test_staff_auth import PASSWORD, _unique_email, _unique_ip, create_staff_user


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[Tenant, uuid.UUID]:
    """Тенант + менеджер, который приглашает."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-a", name="Hotel A")
        session.add(tenant)
        await session.flush()
    manager_id = await create_staff_user(
        _unique_email(), tenant_id=tenant.id, role=StaffRole.MANAGER
    )
    return tenant, manager_id


async def test_accept_creates_user_identity_membership(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(tenant.id, StaffRole.RECEPTIONIST, "Аружан", invited_by=manager_id)
    email = _unique_email()

    result = await accept_invite(grant.invite_token, email=email, password=PASSWORD)

    assert result.created_user
    assert result.role_key is StaffRole.RECEPTIONIST
    # Принявший может войти своим паролем и видит членство.
    session_grant = await login(email, PASSWORD, client_ip=_unique_ip())
    assert session_grant.user_id == result.user_id
    assert session_grant.display_name == "Аружан"
    assert [m.role_key for m in session_grant.memberships] == [StaffRole.RECEPTIONIST]
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, grant.invite_id)
        assert invite is not None
        assert invite.accepted_at is not None
        assert invite.accepted_user_id == result.user_id
        stored = (await session.scalars(select(StaffInvite.token_hash))).all()
    assert grant.invite_token not in stored  # в БД — только хэш


async def test_invite_is_single_use(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Дана", invited_by=manager_id)
    await accept_invite(grant.invite_token, email=_unique_email(), password=PASSWORD)

    with pytest.raises(AppError) as error:
        await accept_invite(grant.invite_token, email=_unique_email(), password=PASSWORD)
    assert error.value.code == ERR_AUTH_INVITE_INVALID


async def test_expired_and_revoked_and_unknown_are_indistinguishable(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel
    expired = await create_invite(tenant.id, StaffRole.STAFF, "Ерлан", invited_by=manager_id)
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, expired.invite_id)
        assert invite is not None
        invite.expires_at = utc_now() - timedelta(seconds=1)
    revoked = await create_invite(tenant.id, StaffRole.STAFF, "Ерлан", invited_by=manager_id)
    await revoke_invite(revoked.invite_id, actor_user_id=manager_id)

    for token in (expired.invite_token, revoked.invite_token, "no-such-token"):
        with pytest.raises(AppError) as error:
            await accept_invite(token, email=_unique_email(), password=PASSWORD)
        assert error.value.code == ERR_AUTH_INVITE_INVALID
        assert error.value.status_code == 410


async def test_revoke_accepted_invite_fails_and_revoke_is_idempotent(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel
    accepted = await create_invite(tenant.id, StaffRole.STAFF, "Али", invited_by=manager_id)
    await accept_invite(accepted.invite_token, email=_unique_email(), password=PASSWORD)
    with pytest.raises(AppError) as error:
        await revoke_invite(accepted.invite_id, actor_user_id=manager_id)
    assert error.value.code == ERR_AUTH_INVITE_INVALID

    pending = await create_invite(tenant.id, StaffRole.STAFF, "Али", invited_by=manager_id)
    await revoke_invite(pending.invite_id, actor_user_id=manager_id)
    await revoke_invite(pending.invite_id, actor_user_id=manager_id)  # повтор — no-op


async def test_existing_email_gets_second_membership_with_password_proof(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    """ADR-008 инвариант а (сеть отелей): существующая личность доказывает
    владение паролем и получает членство; неверный пароль не потребляет инвайт."""
    tenant, manager_id = hotel
    email = _unique_email()
    user_id = await create_staff_user(
        email, tenant_id=tenant.id, role=StaffRole.STAFF, display_name="Существующая"
    )
    async with platform_session_scope() as session:
        hotel_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add(hotel_b)
        await session.flush()
    grant = await create_invite(hotel_b.id, StaffRole.MANAGER, "Новое имя", invited_by=manager_id)

    with pytest.raises(AppError) as error:
        await accept_invite(grant.invite_token, email=email, password="wrong-password!")
    assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, grant.invite_id)
        assert invite is not None and invite.accepted_at is None  # не потреблён

    result = await accept_invite(grant.invite_token, email=email, password=PASSWORD)
    assert not result.created_user
    assert result.user_id == user_id
    async with platform_session_scope() as session:
        user = await session.get(User, user_id)
        assert user is not None and user.display_name == "Существующая"  # имя не перезаписано
        memberships = (
            await session.scalars(
                select(TenantMembership).where(TenantMembership.user_id == user_id)
            )
        ).all()
    assert {m.tenant_id: m.role_key for m in memberships} == {
        tenant.id: StaffRole.STAFF,
        hotel_b.id: StaffRole.MANAGER,
    }


async def test_reinvite_reactivates_revoked_membership_with_new_role(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel
    email = _unique_email()
    user_id = await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    async with platform_session_scope() as session:
        membership = (
            await session.scalars(
                select(TenantMembership).where(TenantMembership.user_id == user_id)
            )
        ).one()
        membership.status = MembershipStatus.REVOKED

    grant = await create_invite(
        tenant.id, StaffRole.RECEPTIONIST, "Возвращенец", invited_by=manager_id
    )
    result = await accept_invite(grant.invite_token, email=email, password=PASSWORD)

    assert not result.created_user
    async with platform_session_scope() as session:
        membership = (
            await session.scalars(
                select(TenantMembership).where(TenantMembership.user_id == user_id)
            )
        ).one()
    assert membership.status is MembershipStatus.ACTIVE
    assert membership.role_key is StaffRole.RECEPTIONIST
    assert membership.invited_by == manager_id


async def test_short_password_rejected(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Ким", invited_by=manager_id)
    with pytest.raises(AppError) as error:
        await accept_invite(grant.invite_token, email=_unique_email(), password="short")
    assert error.value.code == ERR_AUTH_PASSWORD_TOO_SHORT
