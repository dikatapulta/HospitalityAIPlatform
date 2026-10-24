"""Суперадмин (spec 0044 §3–§7, §12): вход с кодом, сброс пароля, стирание.

DB-тесты на временной БД (conftest); rate-limit — на `FakeRateLimitRedis`
(один на тест — счётчик обязан копиться). События уведомлений читаются прямо
из outbox платформенной сессией (политика `platform_dispatch`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.platform.events import SuperadminActionKind
from hospitality.platform.models import (
    MembershipStatus,
    StaffPasswordReset,
    StaffRole,
    StaffSecondFactor,
    StaffSession,
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
    UserIdentityKind,
    UserStatus,
)
from hospitality.platform.staff_auth import (
    ERR_AUTH_FORBIDDEN,
    ERR_AUTH_INVALID_CREDENTIALS,
    ERR_AUTH_USER_NOT_FOUND,
    login,
    resolve_staff_session,
)
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_RATE_LIMITED,
    hash_password,
    password_external_id,
)
from hospitality.platform.staff_invites import create_invite
from hospitality.platform.staff_password_reset import (
    ERR_AUTH_RESET_INVALID,
    accept_password_reset,
    describe_password_reset,
)
from hospitality.platform.staff_superadmin import (
    erase_member,
    issue_password_reset,
    superadmin_login,
)
from hospitality.platform.staff_team import (
    ERR_AUTH_SELF_ACTION,
    ERR_AUTH_SUPERADMIN_TARGET,
    change_member_role,
    deactivate_member,
    list_tenant_members,
)
from hospitality.platform.totp import code_at, generate_secret, time_step
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

NEW_PASSWORD = "new-horse-battery-staple"


@pytest.fixture(autouse=True)
def _counting_rate_limit() -> Iterator[FakeRateLimitRedis]:
    """Свой MonkeyPatch, не общая фикстура: autouse захватил бы её раньше
    `canonical_database`, и undo POSTGRES_DB случился бы после дропа временной
    БД (довод — conftest кабинета)."""
    patcher = pytest.MonkeyPatch()
    fake_redis = FakeRateLimitRedis()
    patcher.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    yield fake_redis
    patcher.undo()


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-s", name="Hotel S")
        session.add(row)
        await session.flush()
        return row


async def create_superadmin(
    tenant_id: uuid.UUID, staff_login: str, *, secret: str | None = None
) -> tuple[uuid.UUID, str]:
    """Суперадмин так, как его заводит `tools/superadmin create`: флаг, логин
    отеля, членство manager и второй фактор. Возвращает (user_id, секрет)."""
    secret = secret or generate_secret()
    secret_hash = await hash_password(PASSWORD)
    async with platform_session_scope() as session:
        user = User(display_name="Основатель", is_platform_admin=True)
        session.add(user)
        await session.flush()
        session.add_all(
            [
                UserIdentity(
                    user_id=user.id,
                    kind=UserIdentityKind.PASSWORD,
                    external_id=password_external_id(tenant_id, staff_login),
                    secret_hash=secret_hash,
                ),
                TenantMembership(user_id=user.id, tenant_id=tenant_id, role_key=StaffRole.MANAGER),
                StaffSecondFactor(user_id=user.id, totp_secret=secret),
            ]
        )
        return user.id, secret


def current_code(secret: str) -> str:
    return code_at(secret, time_step(utc_now()))


async def outbox_kinds() -> list[str]:
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.event_name == "staff.superadmin_acted")
            .order_by(OutboxEvent.occurred_at)
        )
        return [row.payload["kind"] for row in rows]


async def _ip() -> str:
    return f"ip-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


async def test_superadmin_login_creates_short_session_and_announces(tenant: Tenant) -> None:
    staff_login = unique_login()
    user_id, secret = await create_superadmin(tenant.id, staff_login)

    grant = await superadmin_login(
        tenant.id, staff_login, PASSWORD, current_code(secret), client_ip=await _ip()
    )

    assert grant.user_id == user_id
    active = await resolve_staff_session(grant.session_token)
    assert active is not None and active.is_platform_admin
    async with platform_session_scope() as session:
        staff_session = await session.scalar(
            select(StaffSession).where(StaffSession.user_id == user_id)
        )
        factor = await session.get(StaffSecondFactor, user_id)
    assert staff_session is not None
    ttl = staff_session.expires_at - staff_session.created_at
    assert timedelta(minutes=59) < ttl <= timedelta(minutes=60)
    assert factor is not None and factor.last_used_step == time_step(utc_now())
    assert await outbox_kinds() == [SuperadminActionKind.LOGIN.value]


async def test_same_code_twice_is_a_replay(tenant: Tenant) -> None:
    staff_login = unique_login()
    _, secret = await create_superadmin(tenant.id, staff_login)
    code = current_code(secret)
    await superadmin_login(tenant.id, staff_login, PASSWORD, code, client_ip=await _ip())
    with pytest.raises(AppError) as error:
        await superadmin_login(tenant.id, staff_login, PASSWORD, code, client_ip=await _ip())
    assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS


@pytest.mark.parametrize("case", ["bad_password", "unknown_login", "regular_staff", "no_factor"])
async def test_every_rejection_is_one_answer(tenant: Tenant, case: str) -> None:
    staff_login = unique_login()
    _, secret = await create_superadmin(tenant.id, staff_login)
    code, password, attempt_login = current_code(secret), PASSWORD, staff_login
    if case == "bad_password":
        password = "wrong-password-123"
    elif case == "unknown_login":
        attempt_login = unique_login()
    elif case == "regular_staff":
        attempt_login = unique_login()
        await create_staff_user(attempt_login, tenant_id=tenant.id, role=StaffRole.MANAGER)
    else:
        async with platform_session_scope() as session:
            user_id = await session.scalar(
                select(UserIdentity.user_id).where(
                    UserIdentity.external_id == password_external_id(tenant.id, staff_login)
                )
            )
            factor = await session.get(StaffSecondFactor, user_id)
            await session.delete(factor)

    with pytest.raises(AppError) as error:
        await superadmin_login(tenant.id, attempt_login, password, code, client_ip=await _ip())

    assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
    assert error.value.message == "Invalid login, password or code"
    # Уведомление — только «пароль верный, код нет»; здесь не тот случай.
    assert await outbox_kinds() == []


async def test_right_password_wrong_code_announces_and_rejects(tenant: Tenant) -> None:
    staff_login = unique_login()
    _, secret = await create_superadmin(tenant.id, staff_login)
    wrong = str((int(current_code(secret)) + 1) % 1_000_000).zfill(6)

    with pytest.raises(AppError) as error:
        await superadmin_login(tenant.id, staff_login, PASSWORD, wrong, client_ip=await _ip())

    assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
    assert await outbox_kinds() == [SuperadminActionKind.LOGIN_BAD_CODE.value]
    async with platform_session_scope() as session:
        assert await session.scalar(select(StaffSession)) is None


async def test_regular_door_refuses_superadmin_and_spends_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 0044 §3: пароль без кода не открывает кабинет; бюджет — общий."""
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        staff_login = unique_login()
        _, secret = await create_superadmin(tenant.id, staff_login)
        ip = await _ip()
        for _ in range(2):
            with pytest.raises(AppError) as error:
                await login(tenant.id, staff_login, PASSWORD, client_ip=ip)
            assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
        with pytest.raises(AppError) as limited:
            await superadmin_login(
                tenant.id, staff_login, PASSWORD, current_code(secret), client_ip=ip
            )
        assert limited.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Сброс пароля
# ---------------------------------------------------------------------------


async def _staff_member(
    tenant: Tenant, role: StaffRole = StaffRole.RECEPTIONIST
) -> tuple[uuid.UUID, str]:
    staff_login = unique_login()
    return await create_staff_user(staff_login, tenant_id=tenant.id, role=role), staff_login


async def test_reset_link_sets_new_password_and_kills_old_sessions(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    member_id, member_login = await _staff_member(tenant)
    old_grant = await login(tenant.id, member_login, PASSWORD, client_ip=await _ip())

    reset = await issue_password_reset(member_id, tenant.id, actor_user_id=admin_id)

    assert reset.login == member_login
    # До использования ссылки ничего не меняется.
    assert await resolve_staff_session(old_grant.session_token) is not None
    await login(tenant.id, member_login, PASSWORD, client_ip=await _ip())
    invitation = await describe_password_reset(reset.reset_token)
    assert invitation is not None
    assert (invitation.login, invitation.tenant_slug) == (member_login, "hotel-s")

    result = await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)

    assert (result.user_id, result.login) == (member_id, member_login)
    assert await resolve_staff_session(old_grant.session_token) is None
    with pytest.raises(AppError):
        await login(tenant.id, member_login, PASSWORD, client_ip=await _ip())
    await login(tenant.id, member_login, NEW_PASSWORD, client_ip=await _ip())
    # Одноразовая.
    assert await describe_password_reset(reset.reset_token) is None
    with pytest.raises(AppError) as reused:
        await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)
    assert reused.value.code == ERR_AUTH_RESET_INVALID
    assert await outbox_kinds() == [SuperadminActionKind.PASSWORD_RESET_ISSUED.value]


async def test_new_reset_link_replaces_previous(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    member_id, _ = await _staff_member(tenant)
    first = await issue_password_reset(member_id, tenant.id, actor_user_id=admin_id)
    second = await issue_password_reset(member_id, tenant.id, actor_user_id=admin_id)
    assert await describe_password_reset(first.reset_token) is None
    assert await describe_password_reset(second.reset_token) is not None


async def test_expired_reset_link_is_invalid(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    member_id, _ = await _staff_member(tenant)
    reset = await issue_password_reset(member_id, tenant.id, actor_user_id=admin_id)
    async with platform_session_scope() as session:
        row = await session.scalar(select(StaffPasswordReset))
        assert row is not None
        row.expires_at = utc_now() - timedelta(seconds=1)
    with pytest.raises(AppError) as error:
        await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)
    assert error.value.code == ERR_AUTH_RESET_INVALID


async def test_only_superadmin_issues_resets(tenant: Tenant) -> None:
    manager_id, _ = await _staff_member(tenant, StaffRole.MANAGER)
    member_id, _ = await _staff_member(tenant)
    with pytest.raises(AppError) as error:
        await issue_password_reset(member_id, tenant.id, actor_user_id=manager_id)
    assert error.value.code == ERR_AUTH_FORBIDDEN


async def test_reset_targets_refused(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    other_admin_id, _ = await create_superadmin(tenant.id, unique_login())
    with pytest.raises(AppError) as self_error:
        await issue_password_reset(admin_id, tenant.id, actor_user_id=admin_id)
    assert self_error.value.code == ERR_AUTH_SELF_ACTION
    with pytest.raises(AppError) as admin_error:
        await issue_password_reset(other_admin_id, tenant.id, actor_user_id=admin_id)
    assert admin_error.value.code == ERR_AUTH_SUPERADMIN_TARGET

    async with platform_session_scope() as session:
        foreign = Tenant(slug="hotel-foreign", name="Foreign")
        session.add(foreign)
        await session.flush()
        foreign_id = foreign.id
    stranger_id = await create_staff_user(
        unique_login(), tenant_id=foreign_id, role=StaffRole.STAFF
    )
    with pytest.raises(AppError) as foreign_error:
        await issue_password_reset(stranger_id, tenant.id, actor_user_id=admin_id)
    assert foreign_error.value.code == ERR_AUTH_USER_NOT_FOUND


# ---------------------------------------------------------------------------
# Стирание
# ---------------------------------------------------------------------------


async def test_erase_hides_member_closes_login_and_keeps_login_taken(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    member_id, member_login = await _staff_member(tenant)
    grant = await login(tenant.id, member_login, PASSWORD, client_ip=await _ip())
    reset = await issue_password_reset(member_id, tenant.id, actor_user_id=admin_id)

    await erase_member(member_id, tenant.id, actor_user_id=admin_id)

    members = await list_tenant_members(tenant.id)
    assert member_id not in {member.user_id for member in members}
    assert await resolve_staff_session(grant.session_token) is None
    with pytest.raises(AppError) as login_error:
        await login(tenant.id, member_login, PASSWORD, client_ip=await _ip())
    assert login_error.value.code == ERR_AUTH_INVALID_CREDENTIALS
    assert await describe_password_reset(reset.reset_token) is None
    async with platform_session_scope() as session:
        user = await session.get(User, member_id)
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == member_id)
        )
    assert user is not None and user.status is UserStatus.ERASED
    assert membership is not None and membership.status is MembershipStatus.REVOKED
    with pytest.raises(AppError) as taken:
        await create_invite(tenant.id, StaffRole.STAFF, "Тёзка", member_login, invited_by=admin_id)
    assert taken.value.status_code == 409
    assert await outbox_kinds() == [
        SuperadminActionKind.PASSWORD_RESET_ISSUED.value,
        SuperadminActionKind.MEMBER_ERASED.value,
    ]
    with pytest.raises(AppError) as again:
        await erase_member(member_id, tenant.id, actor_user_id=admin_id)
    assert again.value.code == ERR_AUTH_USER_NOT_FOUND


async def test_erase_works_on_deactivated_member(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    manager_id, _ = await _staff_member(tenant, StaffRole.MANAGER)
    member_id, _ = await _staff_member(tenant)
    await deactivate_member(member_id, tenant.id, actor_user_id=manager_id)
    await erase_member(member_id, tenant.id, actor_user_id=admin_id)
    assert member_id not in {member.user_id for member in await list_tenant_members(tenant.id)}


async def test_manager_cannot_erase_or_touch_superadmin(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    manager_id, _ = await _staff_member(tenant, StaffRole.MANAGER)
    member_id, _ = await _staff_member(tenant)
    with pytest.raises(AppError) as erase_error:
        await erase_member(member_id, tenant.id, actor_user_id=manager_id)
    assert erase_error.value.code == ERR_AUTH_FORBIDDEN
    with pytest.raises(AppError) as role_error:
        await change_member_role(admin_id, tenant.id, StaffRole.STAFF, actor_user_id=manager_id)
    assert role_error.value.code == ERR_AUTH_SUPERADMIN_TARGET
    with pytest.raises(AppError) as deactivate_error:
        await deactivate_member(admin_id, tenant.id, actor_user_id=manager_id)
    assert deactivate_error.value.code == ERR_AUTH_SUPERADMIN_TARGET
    superadmin_row = next(
        member for member in await list_tenant_members(tenant.id) if member.user_id == admin_id
    )
    assert superadmin_row.is_platform_admin
