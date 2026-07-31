"""Состав команды тенанта (spec 0033 §7/§10): список, смена роли, отключение.

Смоук страницы и действий — `staff_portal/tests/test_team.py`; здесь — сам
платформенный сервис: тенантная граница, идемпотентность, защита от
самоблокировки, «активность» из живых сессий.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
    StaffSession,
    Tenant,
    TenantMembership,
    UserStatus,
)
from hospitality.platform.staff_auth import ERR_AUTH_USER_NOT_FOUND, login, resolve_staff_session
from hospitality.platform.staff_team import (
    ERR_AUTH_SELF_ACTION,
    change_member_role,
    deactivate_member,
    list_tenant_members,
)
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from tests.test_staff_auth import PASSWORD, _unique_email, _unique_ip, create_staff_user


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Тенант + менеджер, который управляет составом."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-a", name="Hotel A")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    manager_id = await create_staff_user(
        _unique_email(), tenant_id=tenant_id, role=StaffRole.MANAGER, display_name="Менеджер"
    )
    return tenant_id, manager_id


async def test_list_orders_active_first_and_reports_activity(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, manager_id = hotel
    email = _unique_email()
    active_id = await create_staff_user(
        email, tenant_id=tenant_id, role=StaffRole.STAFF, display_name="Айгуль"
    )
    gone_id = await create_staff_user(
        _unique_email(), tenant_id=tenant_id, role=StaffRole.STAFF, display_name="Ержан"
    )
    await deactivate_member(gone_id, tenant_id, actor_user_id=manager_id)
    await login(email, PASSWORD, client_ip=_unique_ip())

    members = await list_tenant_members(tenant_id)

    by_id = {member.user_id: member for member in members}
    assert by_id[active_id].last_active_at is not None
    assert by_id[manager_id].last_active_at is None  # не логинился
    assert by_id[gone_id].user_status is UserStatus.DEACTIVATED
    # Активные выше отключённых, внутри группы — по имени.
    assert [member.user_id for member in members] == [active_id, manager_id, gone_id]


async def test_list_is_scoped_to_tenant(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, _ = hotel
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
        other_id = other.id
    stranger_id = await create_staff_user(
        _unique_email(), tenant_id=other_id, role=StaffRole.MANAGER
    )

    assert stranger_id not in {member.user_id for member in await list_tenant_members(tenant_id)}


async def test_change_role_is_scoped_and_idempotent(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, manager_id = hotel
    member_id = await create_staff_user(_unique_email(), tenant_id=tenant_id, role=StaffRole.STAFF)

    await change_member_role(member_id, tenant_id, StaffRole.RECEPTIONIST, actor_user_id=manager_id)
    await change_member_role(
        member_id, tenant_id, StaffRole.RECEPTIONIST, actor_user_id=manager_id
    )  # повтор — не операция

    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == member_id)
        )
    assert membership is not None and membership.role_key is StaffRole.RECEPTIONIST

    # Чужой тенант той же операции не поддаётся — ERR-AUTH-008.
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
        other_id = other.id
    with pytest.raises(AppError) as error:
        await change_member_role(member_id, other_id, StaffRole.MANAGER, actor_user_id=manager_id)
    assert error.value.code == ERR_AUTH_USER_NOT_FOUND
    assert error.value.status_code == 404


async def test_role_change_of_revoked_membership_is_rejected(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Отключённого нельзя «починить» сменой роли: возврат — новое приглашение."""
    tenant_id, manager_id = hotel
    member_id = await create_staff_user(_unique_email(), tenant_id=tenant_id, role=StaffRole.STAFF)
    await deactivate_member(member_id, tenant_id, actor_user_id=manager_id)

    with pytest.raises(AppError) as error:
        await change_member_role(member_id, tenant_id, StaffRole.MANAGER, actor_user_id=manager_id)
    assert error.value.code == ERR_AUTH_USER_NOT_FOUND


async def test_self_action_is_forbidden(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, manager_id = hotel

    for action in (
        change_member_role(manager_id, tenant_id, StaffRole.STAFF, actor_user_id=manager_id),
        deactivate_member(manager_id, tenant_id, actor_user_id=manager_id),
    ):
        with pytest.raises(AppError) as error:
            await action
        assert error.value.code == ERR_AUTH_SELF_ACTION
        assert error.value.status_code == 409

    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == manager_id)
        )
    assert membership is not None
    assert membership.role_key is StaffRole.MANAGER
    assert membership.status is MembershipStatus.ACTIVE


async def test_deactivation_revokes_sessions_and_memberships(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, manager_id = hotel
    email = _unique_email()
    member_id = await create_staff_user(email, tenant_id=tenant_id, role=StaffRole.STAFF)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    assert await resolve_staff_session(grant.session_token) is not None

    await deactivate_member(member_id, tenant_id, actor_user_id=manager_id)

    assert await resolve_staff_session(grant.session_token) is None
    async with platform_session_scope() as session:
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == member_id)
        )
        sessions = (
            await session.scalars(select(StaffSession).where(StaffSession.user_id == member_id))
        ).all()
    assert membership is not None and membership.status is MembershipStatus.REVOKED
    assert all(staff_session.revoked_at is not None for staff_session in sessions)


async def test_revoked_sessions_do_not_count_as_activity(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Активность» берётся у живых сессий: погашенная не изображает работу."""
    tenant_id, manager_id = hotel
    email = _unique_email()
    member_id = await create_staff_user(email, tenant_id=tenant_id, role=StaffRole.STAFF)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    async with platform_session_scope() as session:
        staff_session = await session.scalar(
            select(StaffSession).where(StaffSession.user_id == member_id)
        )
        assert staff_session is not None
        staff_session.revoked_at = utc_now()
        staff_session.last_used_at = utc_now() - timedelta(days=3)
    assert grant.session_token

    members = {member.user_id: member for member in await list_tenant_members(tenant_id)}
    assert members[member_id].last_active_at is None
    assert manager_id in members
