"""Бюджет попыток входа: параллельная пачка и знакомые устройства учётки (#1133, #1134, #1060).

Попытка занимает место в ключах до проверки пароля и возвращается, если пароль
доказан; знакомые браузеры одной учётки делят один ключ `staff_login_device`,
и дверь суперадмина знает их так же, как обычная. Rate-limit — на
`FakeRateLimitRedis` (канон 0023).
"""

from __future__ import annotations

import asyncio

import pytest

from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import ERR_AUTH_INVALID_CREDENTIALS, login
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.platform.staff_superadmin import superadmin_login
from hospitality.platform.totp import code_at, time_step
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from tests.test_staff_auth import (
    PASSWORD,
    _counting_redis,
    _unique_ip,
    create_staff_user,
    spent_scopes,
    unique_login,
)
from tests.test_staff_superadmin import create_superadmin

WRONG = "wrong-password!"


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-b", name="Hotel B")
        session.add(row)
        await session.flush()
        return row


async def _outcome(
    tenant: Tenant, staff_login: str, password: str, *, ip: str, device: str | None = None
) -> str:
    try:
        await login(tenant.id, staff_login, password, client_ip=ip, device_token=device)
    except AppError as error:
        return error.code
    return "ok"


async def test_parallel_batch_checks_no_more_passwords_than_the_limit(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1133: пачка запросов разом проверяет не больше паролей, чем лимит.

    Прежняя пара «прочитать до пароля — списать после отказа» пропускала всю
    пачку: каждый запрос читал счётчик раньше, чем первый из них списывал."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    fake_redis = _counting_redis(monkeypatch, account=3, ip=60)
    try:
        ip = _unique_ip()
        outcomes = await asyncio.gather(
            *(_outcome(tenant, staff_login, WRONG, ip=ip) for _ in range(8))
        )
        assert outcomes.count(ERR_AUTH_INVALID_CREDENTIALS) == 3
        assert outcomes.count(ERR_AUTH_LOGIN_RATE_LIMITED) == 5
        # Отказ лимитом свой резерв вернул: счёт — ровно на пределе.
        account = [n for key, n in fake_redis.counters.items() if "staff_login_account" in key]
        assert account == [3]
    finally:
        get_settings.cache_clear()


async def test_parallel_successful_logins_still_spend_nothing(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#207 после #1133: резерв успешного входа возвращается."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    fake_redis = _counting_redis(monkeypatch, account=5, ip=60)
    try:
        ip = _unique_ip()
        outcomes = await asyncio.gather(
            *(_outcome(tenant, staff_login, PASSWORD, ip=ip) for _ in range(5))
        )
        assert outcomes == ["ok"] * 5
        assert spent_scopes(fake_redis) == set()
    finally:
        get_settings.cache_clear()


async def test_known_browsers_of_one_account_share_one_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1134, #1060: заранее заведённые браузеры не множат попытки подбора —
    знакомые устройства учётки делят один ключ."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    _counting_redis(monkeypatch, account=2, ip=60)
    try:
        ip = _unique_ip()
        first = (await login(tenant.id, staff_login, PASSWORD, client_ip=ip)).device_token
        second = (await login(tenant.id, staff_login, PASSWORD, client_ip=ip)).device_token
        assert first != second
        for _ in range(2):
            outcome = await _outcome(tenant, staff_login, WRONG, ip=ip, device=first)
            assert outcome == ERR_AUTH_INVALID_CREDENTIALS

        outcome = await _outcome(tenant, staff_login, WRONG, ip=ip, device=second)
        assert outcome == ERR_AUTH_LOGIN_RATE_LIMITED
    finally:
        get_settings.cache_clear()


async def test_superadmin_enters_from_known_browser_when_account_key_is_locked(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1134: неверные пароли к логину суперадмина с любого адреса запирают ключ
    учётки, но не браузер, где суперадмин уже входил своей дверью."""
    staff_login = unique_login()
    _, secret = await create_superadmin(tenant.id, staff_login)
    _counting_redis(monkeypatch, account=2, ip=60)
    try:
        step = time_step(utc_now())
        grant = await superadmin_login(
            tenant.id, staff_login, PASSWORD, code_at(secret, step), client_ip=_unique_ip()
        )
        for _ in range(2):
            outcome = await _outcome(tenant, staff_login, WRONG, ip=_unique_ip())
            assert outcome == ERR_AUTH_INVALID_CREDENTIALS
        next_code = code_at(secret, step + 1)
        with pytest.raises(AppError) as error:
            await superadmin_login(
                tenant.id, staff_login, PASSWORD, next_code, client_ip=_unique_ip()
            )
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED

        again = await superadmin_login(
            tenant.id,
            staff_login,
            PASSWORD,
            next_code,
            client_ip=_unique_ip(),
            device_token=grant.device_token,
        )
        assert again.device_token == grant.device_token
    finally:
        get_settings.cache_clear()
