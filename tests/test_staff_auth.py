"""Аутентификация персонала (spec 0033 §3, §10; spec 0037 §2–§3, §10; ADR-008 §1):
логин отеля/login/сессии/require_role/деактивация.

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
    find_hotel,
    login,
    logout,
    require_role,
    resolve_staff_session,
)
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_INVALID,
    ERR_AUTH_LOGIN_RATE_LIMITED,
    ERR_AUTH_PASSWORD_TOO_SHORT,
    hash_password,
    normalize_login,
    parse_password_external_id,
    password_external_id,
    verify_password,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

PASSWORD = "correct-horse-battery"


def unique_login() -> str:
    # Уникальный логин на тест: реальный Redis локальной среды (make dev) не
    # должен копить rate-limit между тестами. Формат spec 0037 §2.
    return f"U{uuid.uuid4().hex[:8].upper()}"


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
    staff_login: str,
    *,
    tenant_id: uuid.UUID,
    role: StaffRole,
    password: str = PASSWORD,
    display_name: str = "Test Staff",
) -> uuid.UUID:
    """Прямое создание сотрудника для тестов (боевые пути — bootstrap и инвайт):
    идентичность с логином отеля `tenant_id` (spec 0037 §6)."""
    secret_hash = await hash_password(password)
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        session.add(
            UserIdentity(
                user_id=user.id,
                kind=UserIdentityKind.PASSWORD,
                external_id=password_external_id(tenant_id, staff_login.upper()),
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  borm ", "BORM"),  # регистр и пробелы не важны
        ("Borm2", "BORM2"),
        ("abc", "ABC"),  # нижняя граница длины
        ("A" * 12, "A" * 12),  # верхняя
        ("ab", None),
        ("A" * 13, None),
        ("2BORM", None),  # цифра первой
        ("БОРМ", None),  # кириллица
        ("ИЩКЬ", None),  # латинский логин в русской раскладке
        ("BO RM", None),
        ("BO-RM", None),
        ("", None),
        # Не-ASCII, который upper() превратил бы в латиницу: ß → SS, ı → I.
        ("ßabc", None),
        ("ıvan", None),
    ],
)
def test_normalize_login(raw: str, expected: str | None) -> None:
    assert normalize_login(raw) == expected


def test_password_external_id_roundtrip() -> None:
    tenant_id = uuid.uuid4()
    external_id = password_external_id(tenant_id, "BORM")
    assert external_id == f"{tenant_id}:BORM"
    assert parse_password_external_id(external_id) == (tenant_id, "BORM")
    for foreign in ("manager@hotel.kz", "BORM", f"{tenant_id}:borm", "not-a-uuid:BORM"):
        assert parse_password_external_id(foreign) is None


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
    staff_login = unique_login()
    user_id = await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.MANAGER)

    # Регистр и пробелы в логине терпимы (нормализация spec 0037 §2).
    grant = await login(tenant.id, f"  {staff_login.lower()} ", PASSWORD, client_ip=_unique_ip())

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
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)

    for attempt_login, password in ((staff_login, "wrong-password!"), (unique_login(), PASSWORD)):
        with pytest.raises(AppError) as error:
            await login(tenant.id, attempt_login, password, client_ip=_unique_ip())
        assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
        assert error.value.status_code == 401


async def test_same_login_in_two_hotels_are_two_accounts(tenant: Tenant) -> None:
    """Spec 0037 §2: логин уникален внутри отеля — `BORM` соседнего отеля
    другая учётка, и его пароль в этом отеле не пускает."""
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
    here = await create_staff_user("BORM", tenant_id=tenant.id, role=StaffRole.STAFF)
    there = await create_staff_user(
        "BORM", tenant_id=other.id, role=StaffRole.STAFF, password="other-hotel-password"
    )

    with pytest.raises(AppError) as error:
        await login(tenant.id, "BORM", "other-hotel-password", client_ip=_unique_ip())
    assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
    assert (await login(tenant.id, "BORM", PASSWORD, client_ip=_unique_ip())).user_id == here
    assert (
        await login(other.id, "borm", "other-hotel-password", client_ip=_unique_ip())
    ).user_id == there


async def test_find_hotel_is_exact_slug_match(tenant: Tenant) -> None:
    """Код отеля к slug приводит страница (trim + нижний регистр); сам поиск
    точный — иначе slug со заглавными стал бы недостижим по своему адресу."""
    hotel = await find_hotel(tenant.slug)
    assert hotel is not None
    assert (hotel.tenant_id, hotel.slug, hotel.name) == (tenant.id, tenant.slug, tenant.name)
    assert await find_hotel(tenant.slug.upper()) is None
    assert await find_hotel("no-such-hotel") is None
    # Заведомо не slug — без похода в БД (NUL Postgres не принял бы вовсе).
    assert await find_hotel("bad\x00slug") is None
    assert await find_hotel("a" * 64) is None


async def test_login_deactivated_user_rejected(tenant: Tenant) -> None:
    staff_login = unique_login()
    user_id = await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    await deactivate_user(user_id, actor_user_id=user_id)

    with pytest.raises(AppError) as error:
        await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
    assert error.value.code == ERR_AUTH_USER_DEACTIVATED


def _counting_redis(
    monkeypatch: pytest.MonkeyPatch, *, account: int, ip: int
) -> FakeRateLimitRedis:
    """Один фейк Redis на тест (счётчик должен накапливаться) и свои лимиты;
    вызывающий обязан сбросить кэш настроек в finally."""
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", str(account))
    monkeypatch.setenv("STAFF_LOGIN_IP_RATE_LIMIT_ATTEMPTS", str(ip))
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    get_settings.cache_clear()
    return fake_redis


async def test_login_wrong_format_is_hint_without_spending_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 0037 §3: логин не того формата отклоняется ДО поиска подсказкой
    ERR-AUTH-012 и бюджет не тратит — пароль не проверялся, формат не секрет."""
    fake_redis = _counting_redis(monkeypatch, account=1, ip=1)
    try:
        for typo in ("ИЩКЬ", "2BORM", "ab", "A" * 13):
            with pytest.raises(AppError) as error:
                await login(tenant.id, typo, PASSWORD, client_ip=_unique_ip())
            assert error.value.code == ERR_AUTH_LOGIN_INVALID
            assert error.value.status_code == 422
        assert fake_redis.counters == {}
    finally:
        get_settings.cache_clear()


async def test_login_rate_limited_by_account_and_ip(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 0033 §3.3 / 0037 §3: два ключа — подбор пароля к паре «отель +
    логин» И перебор учёток с IP."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    fake_redis = _counting_redis(monkeypatch, account=2, ip=2)
    try:
        ip = _unique_ip()
        for _ in range(2):
            with pytest.raises(AppError) as error:
                await login(tenant.id, staff_login, "wrong-password!", client_ip=ip)
            assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS
        with pytest.raises(AppError) as error:
            await login(tenant.id, staff_login, PASSWORD, client_ip=ip)  # даже верный пароль
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
        # Другой логин с того же IP — второй ключ тоже держит.
        with pytest.raises(AppError) as error:
            await login(tenant.id, unique_login(), PASSWORD, client_ip=ip)
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
        # Ключ учётки — scope `staff_login_account`, логин в ключах Redis не
        # светится (PII вне Redis — хэш пары «отель + логин»).
        assert any("staff_login_account" in key for key in fake_redis.counters)
        assert not any(staff_login in key for key in fake_redis.counters)
    finally:
        get_settings.cache_clear()


async def test_account_budget_is_per_hotel(tenant: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ учётки — пара «отель + логин»: подбор к `BORM` одного отеля не
    запирает `BORM` соседнего (spec 0037 §3)."""
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
    await create_staff_user("BORM", tenant_id=tenant.id, role=StaffRole.STAFF)
    await create_staff_user("BORM", tenant_id=other.id, role=StaffRole.STAFF)
    _counting_redis(monkeypatch, account=1, ip=0)
    try:
        with pytest.raises(AppError):
            await login(tenant.id, "BORM", "wrong-password!", client_ip=_unique_ip())
        with pytest.raises(AppError) as error:
            await login(tenant.id, "BORM", PASSWORD, client_ip=_unique_ip())
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
        assert await login(other.id, "BORM", PASSWORD, client_ip=_unique_ip())
    finally:
        get_settings.cache_clear()


async def test_successful_logins_do_not_spend_login_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #207: бюджет тратят только неудачи.

    Иначе день раздачи доступов умирал на одиннадцатом сотруднике: за
    туннелем весь отель приходит с одного адреса, и успешные входы съедали
    общий IP-ключ.
    """
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    fake_redis = _counting_redis(monkeypatch, account=2, ip=2)
    try:
        ip = _unique_ip()
        for _ in range(5):
            assert await login(tenant.id, staff_login, PASSWORD, client_ip=ip)

        assert fake_redis.counters == {}  # ни одной списанной попытки
    finally:
        get_settings.cache_clear()


async def test_deactivated_user_does_not_spend_login_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ доказавшему пароль подбором не является (issue #207): иначе
    телефон уволенного сотрудника, сам повторяющий вход, выжигал бы лимит
    живой смене за тем же NAT."""
    staff_login = unique_login()
    user_id = await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    await deactivate_user(user_id, actor_user_id=user_id)
    fake_redis = _counting_redis(monkeypatch, account=2, ip=2)
    try:
        ip = _unique_ip()
        for _ in range(3):
            with pytest.raises(AppError) as error:
                await login(tenant.id, staff_login, PASSWORD, client_ip=ip)
            assert error.value.code == ERR_AUTH_USER_DEACTIVATED

        assert fake_redis.counters == {}
    finally:
        get_settings.cache_clear()


async def test_ip_budget_is_wider_than_account_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #207: у ключей разные субъекты — за учёткой один человек, за IP
    весь отель за NAT, поэтому и бюджеты разные. Три сотрудника ошиблись
    паролем по разу — IP-ключ ещё держит смену."""
    _counting_redis(monkeypatch, account=1, ip=5)
    try:
        ip = _unique_ip()
        for _ in range(3):
            mistyping = unique_login()
            await create_staff_user(mistyping, tenant_id=tenant.id, role=StaffRole.STAFF)
            with pytest.raises(AppError) as error:
                await login(tenant.id, mistyping, "wrong-password!", client_ip=ip)
            assert error.value.code == ERR_AUTH_INVALID_CREDENTIALS

        # Четвёртый сотрудник с того же адреса входит штатно.
        arriving = unique_login()
        await create_staff_user(arriving, tenant_id=tenant.id, role=StaffRole.STAFF)
        assert await login(tenant.id, arriving, PASSWORD, client_ip=ip)
    finally:
        get_settings.cache_clear()


async def test_session_idle_and_absolute_expiry(tenant: Tenant) -> None:
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    idle_days = get_settings().staff_session_idle_ttl_days

    idle_grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
    absolute_grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
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
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
    stale = utc_now() - timedelta(hours=1)
    async with platform_session_scope() as session:
        row = (await session.scalars(select(StaffSession))).one()
        row.last_used_at = stale

    assert await resolve_staff_session(grant.session_token) is not None
    async with platform_session_scope() as session:
        refreshed = (await session.scalars(select(StaffSession))).one()
    assert refreshed.last_used_at > stale  # активность продлила idle-срок


async def test_logout_revokes_session_idempotently(tenant: Tenant) -> None:
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())

    await logout(grant.session_token)
    assert await resolve_staff_session(grant.session_token) is None
    await logout(grant.session_token)  # повтор — no-op
    await logout("no-such-token")


async def test_deactivation_is_one_transaction(tenant: Tenant) -> None:
    """DoD #48: деактивация гасит сессии и членства разом; вход закрыт."""
    staff_login = unique_login()
    actor_id = await create_staff_user(unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER)
    user_id = await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())

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
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=role)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
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
    staff_login = unique_login()
    user_id = await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.MANAGER)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
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
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.MANAGER)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
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
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.MANAGER)
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())

    with pytest.raises(RuntimeError, match="tenant_slug"):
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, None))


def test_require_role_needs_at_least_one_role() -> None:
    with pytest.raises(ValueError, match="at least one role"):
        require_role()


async def test_platform_admin_gets_no_implicit_access(tenant: Tenant) -> None:
    """ADR-008 §1: `is_platform_admin` — не членство; require_role его не пускает
    (рекомендация ревью PR #148 — security-свойство закреплено тестом)."""
    staff_login = unique_login()
    secret_hash = await hash_password(PASSWORD)
    async with platform_session_scope() as session:
        admin = User(display_name="Platform Admin", is_platform_admin=True)
        session.add(admin)
        await session.flush()
        session.add(
            UserIdentity(
                user_id=admin.id,
                kind=UserIdentityKind.PASSWORD,
                external_id=password_external_id(tenant.id, staff_login),
                secret_hash=secret_hash,
            )
        )
    grant = await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
    assert grant.memberships == []

    with pytest.raises(AppError) as error:
        await require_role(*QUEUE_ROLES)(_staff_request(grant.session_token, tenant.slug))
    assert error.value.code == ERR_AUTH_FORBIDDEN
