"""Аутентификация персонала (spec 0033 §3, §10; ADR-008 §1): login/сессии/
require_role/деактивация.

DB-тесты — на временной БД (conftest); rate-limit — на `FakeRateLimitRedis`
(канон 0023: в CI живого Redis нет, без подмены лимит уходит в fail-open).
`require_role` проверяется прямым вызовом зависимости на сфабрикованном
Request — той же самой, что получит Depends(...) страницы кабинета (PR C).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import Request
from sqlalchemy import select

from hospitality.platform.models import (
    MembershipStatus,
    StaffRole,
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
    ERR_AUTH_SESSION_INVALID,
    ERR_AUTH_USER_DEACTIVATED,
    STAFF_SESSION_COOKIE,
    StaffContext,
    deactivate_user,
    login,
    logout,
    require_role,
    resolve_staff_session,
)
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_RATE_LIMITED,
    ERR_AUTH_PASSWORD_TOO_SHORT,
    hash_password,
    normalize_email,
    verify_password,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

PASSWORD = "correct-horse-battery"


def _unique_email() -> str:
    # Уникальный email на тест: реальный Redis локальной среды (make dev) не
    # должен копить rate-limit между тестами.
    return f"user-{uuid.uuid4().hex[:8]}@hotel.kz"


def _unique_ip() -> str:
    return f"ip-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-a", name="Hotel A")
        session.add(row)
        await session.flush()
        return row


async def create_staff_user(
    email: str,
    *,
    tenant_id: uuid.UUID,
    role: StaffRole,
    password: str = PASSWORD,
    display_name: str = "Test Staff",
) -> uuid.UUID:
    """Прямое создание сотрудника для тестов (боевые пути — bootstrap и инвайт)."""
    secret_hash = await hash_password(password)
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        session.add(
            UserIdentity(
                user_id=user.id,
                kind=UserIdentityKind.PASSWORD,
                external_id=normalize_email(email),
                secret_hash=secret_hash,
            )
        )
        session.add(TenantMembership(user_id=user.id, tenant_id=tenant_id, role_key=role))
        return user.id


def _staff_request(token: str | None, tenant_slug: str | None) -> Request:
    """Сфабрикованный Request: ровно то, что видит зависимость require_role."""
    headers = []
    if token is not None:
        headers.append((b"cookie", f"{STAFF_SESSION_COOKIE}={token}".encode()))
    path_params: dict[str, str] = {}
    if tenant_slug is not None:
        path_params["tenant_slug"] = tenant_slug
    return Request({"type": "http", "headers": headers, "path_params": path_params})


# ---------------------------------------------------------------------------
# Чистые юниты (без БД)
# ---------------------------------------------------------------------------


def test_normalize_email() -> None:
    assert normalize_email("  Aruzhan@Hotel.KZ ") == "aruzhan@hotel.kz"


async def test_password_hash_roundtrip_and_min_length() -> None:
    secret_hash = await hash_password(PASSWORD)
    assert PASSWORD not in secret_hash  # в БД — только argon2-хэш
    assert secret_hash.startswith("$argon2id$")
    assert await verify_password(PASSWORD, secret_hash)
    assert not await verify_password("wrong-password", secret_hash)
    assert not await verify_password(PASSWORD, "not-a-hash")

    with pytest.raises(AppError) as error:
        await hash_password("short")
    assert error.value.code == ERR_AUTH_PASSWORD_TOO_SHORT


# ---------------------------------------------------------------------------
# Login и сессии
# ---------------------------------------------------------------------------


async def test_login_grants_session_and_memberships(tenant: Tenant) -> None:
    email = _unique_email()
    user_id = await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.MANAGER)

    # Регистр и пробелы в email терпимы (нормализация §3.1).
    grant = await login(f"  {email.upper()} ", PASSWORD, client_ip=_unique_ip())

    assert grant.user_id == user_id
    assert [m.tenant_slug for m in grant.memberships] == [tenant.slug]
    assert grant.memberships[0].role_key is StaffRole.MANAGER

    active = await resolve_staff_session(grant.session_token)
    assert active is not None
    assert active.user_id == user_id
    # В БД токен не хранится в открытом виде.
    async with platform_session_scope() as session:
        stored = (await session.scalars(select(StaffSession.token_hash))).all()
    assert grant.session_token not in stored


async def test_login_rejections_are_indistinguishable(tenant: Tenant) -> None:
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)

    for attempt_email, password in ((email, "wrong-password!"), (_unique_email(), PASSWORD)):
        with pytest.raises(AppError) as error:
            await login(attempt_email, password, client_ip=_unique_ip())
        assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
        assert error.value.status_code == 401


async def test_login_deactivated_user_rejected(tenant: Tenant) -> None:
    email = _unique_email()
    user_id = await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    await deactivate_user(user_id, actor_user_id=user_id)

    with pytest.raises(AppError) as error:
        await login(email, PASSWORD, client_ip=_unique_ip())
    assert error.value.code == ERR_AUTH_USER_DEACTIVATED


async def test_login_rate_limited_by_email_and_ip(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 0033 §3.3: два ключа — подбор пароля к email И перебор учёток с IP."""
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", "2")
    fake_redis = FakeRateLimitRedis()  # один на тест: счётчик должен накапливаться
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    get_settings.cache_clear()
    try:
        ip = _unique_ip()
        for _ in range(2):
            with pytest.raises(AppError) as error:
                await login(email, "wrong-password!", client_ip=ip)
            assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
        with pytest.raises(AppError) as error:
            await login(email, PASSWORD, client_ip=ip)  # даже верный пароль
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
        # Другой email с того же IP — второй ключ тоже держит.
        with pytest.raises(AppError) as error:
            await login(_unique_email(), PASSWORD, client_ip=ip)
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
        # Email в ключах Redis не светится (PII вне Redis — хэш).
        assert not any(email in key for key in fake_redis.counters)
    finally:
        get_settings.cache_clear()


async def test_session_idle_and_absolute_expiry(tenant: Tenant) -> None:
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    idle_days = get_settings().staff_session_idle_ttl_days

    idle_grant = await login(email, PASSWORD, client_ip=_unique_ip())
    absolute_grant = await login(email, PASSWORD, client_ip=_unique_ip())
    active_idle = await resolve_staff_session(idle_grant.session_token)
    assert active_idle is not None
    async with platform_session_scope() as session:
        for row in (await session.scalars(select(StaffSession))).all():
            if row.id == active_idle.session_id:
                row.last_used_at = utc_now() - timedelta(days=idle_days, hours=1)
            else:
                row.expires_at = utc_now() - timedelta(seconds=1)

    assert await resolve_staff_session(idle_grant.session_token) is None
    assert await resolve_staff_session(absolute_grant.session_token) is None


async def test_activity_refreshes_idle_timer(tenant: Tenant) -> None:
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    stale = utc_now() - timedelta(hours=1)
    async with platform_session_scope() as session:
        row = (await session.scalars(select(StaffSession))).one()
        row.last_used_at = stale

    assert await resolve_staff_session(grant.session_token) is not None
    async with platform_session_scope() as session:
        refreshed = (await session.scalars(select(StaffSession))).one()
    assert refreshed.last_used_at > stale  # активность продлила idle-срок


async def test_logout_revokes_session_idempotently(tenant: Tenant) -> None:
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())

    await logout(grant.session_token)
    assert await resolve_staff_session(grant.session_token) is None
    await logout(grant.session_token)  # повтор — no-op
    await logout("no-such-token")


async def test_deactivation_is_one_transaction(tenant: Tenant) -> None:
    """DoD #48: деактивация гасит сессии и членства разом; вход закрыт."""
    email = _unique_email()
    actor_id = await create_staff_user(_unique_email(), tenant_id=tenant.id, role=StaffRole.MANAGER)
    user_id = await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.STAFF)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())

    await deactivate_user(user_id, actor_user_id=actor_id)

    assert await resolve_staff_session(grant.session_token) is None
    async with platform_session_scope() as session:
        user = await session.get(User, user_id)
        assert user is not None and user.status is UserStatus.DEACTIVATED
        membership = (
            await session.scalars(
                select(TenantMembership).where(TenantMembership.user_id == user_id)
            )
        ).one()
        assert membership.status is MembershipStatus.REVOKED
        revoked = (
            await session.scalars(select(StaffSession).where(StaffSession.user_id == user_id))
        ).all()
        assert all(row.revoked_at is not None for row in revoked)


async def test_deactivate_unknown_user_fails(tenant: Tenant) -> None:
    with pytest.raises(AppError) as error:
        await deactivate_user(uuid.uuid4(), actor_user_id=uuid.uuid4())
    assert error.value.status_code == 404


# ---------------------------------------------------------------------------
# require_role — мини-матрица §3.2
# ---------------------------------------------------------------------------

# Колонки матрицы: очередь (все роли), заселение, «Сотрудники» (spec 0033 §3.2).
QUEUE_ROLES = (StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER)
CHECKIN_ROLES = (StaffRole.RECEPTIONIST, StaffRole.MANAGER)
TEAM_ROLES = (StaffRole.MANAGER,)


@pytest.mark.parametrize(
    ("role", "allowed", "granted"),
    [
        (StaffRole.STAFF, QUEUE_ROLES, True),
        (StaffRole.STAFF, CHECKIN_ROLES, False),
        (StaffRole.STAFF, TEAM_ROLES, False),
        (StaffRole.RECEPTIONIST, CHECKIN_ROLES, True),
        (StaffRole.RECEPTIONIST, TEAM_ROLES, False),
        (StaffRole.MANAGER, TEAM_ROLES, True),
    ],
)
async def test_require_role_mini_matrix(
    tenant: Tenant, role: StaffRole, allowed: tuple[StaffRole, ...], granted: bool
) -> None:
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=role)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    request = _staff_request(grant.session_token, tenant.slug)

    if granted:
        # Успешный путь — только при совпадающем RLS-контексте запроса
        # (fail-closed сверка в require_role, рекомендация ревью PR #153).
        with tenant_context(tenant.id):
            context = await require_role(*allowed)(request)
        assert isinstance(context, StaffContext)
        assert context.role_key is role
        assert context.tenant_id == tenant.id
    else:
        with pytest.raises(AppError) as error:
            await require_role(*allowed)(request)
        assert error.value.code == ERR_AUTH_FORBIDDEN
        assert error.value.status_code == 403


async def test_require_role_without_session_is_401(tenant: Tenant) -> None:
    for token in (None, "garbage-token"):
        with pytest.raises(AppError) as error:
            await require_role(*QUEUE_ROLES)(_staff_request(token, tenant.slug))
        assert error.value.code == ERR_AUTH_SESSION_INVALID
        assert error.value.status_code == 401


async def test_require_role_foreign_tenant_and_revoked_membership(tenant: Tenant) -> None:
    """Резолвер-инвариант §10: slug без членства → 403; revoked закрывает доступ
    следующим же запросом (сессия при этом жива — она принадлежит личности)."""
    email = _unique_email()
    user_id = await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.MANAGER)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    async with platform_session_scope() as session:
        session.add(Tenant(slug="hotel-b", name="Hotel B"))

    with pytest.raises(AppError) as error:
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, "hotel-b"))
    assert error.value.code == ERR_AUTH_FORBIDDEN

    async with platform_session_scope() as session:
        membership = (
            await session.scalars(
                select(TenantMembership).where(TenantMembership.user_id == user_id)
            )
        ).one()
        membership.status = MembershipStatus.REVOKED
    with pytest.raises(AppError) as error:
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, tenant.slug))
    assert error.value.code == ERR_AUTH_FORBIDDEN
    assert await resolve_staff_session(grant.session_token) is not None


async def test_require_role_tenant_context_mismatch_fails_closed(tenant: Tenant) -> None:
    """Ревью PR #153: авторизация прошла, но RLS-контекст запроса чужой или не
    установлен (например, тенанта поставило другое звено цепочки по
    SERVICE_TOKEN) → 403, а не действие под чужим контекстом БД."""
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.MANAGER)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    request = _staff_request(grant.session_token, tenant.slug)

    with pytest.raises(AppError) as error:  # контекст не установлен вовсе
        await require_role(*QUEUE_ROLES)(request)
    assert error.value.code == ERR_AUTH_FORBIDDEN

    # Контекст чужого тенанта — тоже отказ.
    with tenant_context(uuid.uuid4()), pytest.raises(AppError) as error:
        await require_role(*QUEUE_ROLES)(request)
    assert error.value.code == ERR_AUTH_FORBIDDEN
    assert error.value.status_code == 403


async def test_require_role_route_without_slug_is_programmer_error(tenant: Tenant) -> None:
    email = _unique_email()
    await create_staff_user(email, tenant_id=tenant.id, role=StaffRole.MANAGER)
    grant = await login(email, PASSWORD, client_ip=_unique_ip())

    with pytest.raises(RuntimeError, match="tenant_slug"):
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, None))


def test_require_role_needs_at_least_one_role() -> None:
    with pytest.raises(ValueError, match="at least one role"):
        require_role()


async def test_platform_admin_gets_no_implicit_access(tenant: Tenant) -> None:
    """ADR-008 §1: `is_platform_admin` — не членство; require_role его не пускает
    (рекомендация ревью PR #148 — security-свойство закреплено тестом)."""
    email = _unique_email()
    secret_hash = await hash_password(PASSWORD)
    async with platform_session_scope() as session:
        admin = User(display_name="Platform Admin", is_platform_admin=True)
        session.add(admin)
        await session.flush()
        session.add(
            UserIdentity(
                user_id=admin.id,
                kind=UserIdentityKind.PASSWORD,
                external_id=normalize_email(email),
                secret_hash=secret_hash,
            )
        )
    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    assert grant.memberships == []

    with pytest.raises(AppError) as error:
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, tenant.slug))
    assert error.value.code == ERR_AUTH_FORBIDDEN
