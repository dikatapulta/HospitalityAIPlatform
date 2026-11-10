"""Знакомое устройство входа кабинета (spec 0033 §3.3, issue #402).

Код отеля публичен, логин угадывается по бейджу, адрес у отеля с гостевым
Wi-Fi общий: гость исчерпывал ключи учётки и IP и запирал вход персоналу.
Браузер, где по логину уже входили, идёт мимо них со своим ключом
`staff_login_device`. Rate-limit — на `FakeRateLimitRedis` (канон 0023).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import ERR_AUTH_INVALID_CREDENTIALS, login
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.platform.staff_devices import (
    KNOWN_DEVICE_TTL_DAYS,
    StaffKnownDevice,
    known_device_key,
)
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

WRONG = "wrong-password!"


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-a", name="Hotel A")
        session.add(row)
        await session.flush()
        return row


async def _rejected(
    tenant_id: uuid.UUID,
    staff_login: str,
    password: str,
    *,
    client_ip: str,
    device_token: str | None = None,
) -> str:
    with pytest.raises(AppError) as error:
        await login(
            tenant_id, staff_login, password, client_ip=client_ip, device_token=device_token
        )
    return error.value.code


async def test_guest_cannot_lock_out_staff_on_known_device(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гость с бейджа знает логин и 10 неверными паролями запирает учётку —
    но не браузер стойки, где сотрудник уже входил."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    fake_redis = _counting_redis(monkeypatch, account=2, ip=60)
    try:
        hotel_ip = _unique_ip()
        device = (await login(tenant.id, staff_login, PASSWORD, client_ip=hotel_ip)).device_token
        for _ in range(2):
            code = await _rejected(tenant.id, staff_login, WRONG, client_ip=hotel_ip)
            assert code == ERR_AUTH_INVALID_CREDENTIALS
        # Незнакомый браузер — заперт, даже с верным паролем.
        code = await _rejected(tenant.id, staff_login, PASSWORD, client_ip=hotel_ip)
        assert code == ERR_AUTH_LOGIN_RATE_LIMITED

        grant = await login(
            tenant.id, staff_login, PASSWORD, client_ip=hotel_ip, device_token=device
        )
        assert grant.device_token == device  # один токен на браузер
        assert "staff_login_device" not in spent_scopes(fake_redis)
    finally:
        get_settings.cache_clear()


async def test_guest_cannot_lock_out_whole_hotel_for_known_devices(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Поток выдуманных логинов и подбор к логинам с бейджей запирают адрес
    отеля; знакомые устройства персонала за тем же адресом входят."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    badge_login = unique_login()
    await create_staff_user(badge_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    _counting_redis(monkeypatch, account=10, ip=2, flood=3)
    try:
        hotel_ip = _unique_ip()
        device = (await login(tenant.id, staff_login, PASSWORD, client_ip=hotel_ip)).device_token
        for _ in range(2):
            await _rejected(tenant.id, badge_login, WRONG, client_ip=hotel_ip)
        await _rejected(tenant.id, unique_login(), WRONG, client_ip=hotel_ip)

        code = await _rejected(tenant.id, staff_login, PASSWORD, client_ip=hotel_ip)
        assert code == ERR_AUTH_LOGIN_RATE_LIMITED
        assert await login(
            tenant.id, staff_login, PASSWORD, client_ip=hotel_ip, device_token=device
        )
    finally:
        get_settings.cache_clear()


async def test_known_device_failures_spend_only_its_own_budget(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Подбор со знакомого устройства не быстрее ключа учётки: у пары
    «устройство + учётка» тот же бюджет, и он запирается."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    fake_redis = _counting_redis(monkeypatch, account=2, ip=60)
    try:
        ip = _unique_ip()
        device = (await login(tenant.id, staff_login, PASSWORD, client_ip=ip)).device_token
        for _ in range(2):
            code = await _rejected(tenant.id, staff_login, WRONG, client_ip=ip, device_token=device)
            assert code == ERR_AUTH_INVALID_CREDENTIALS
        assert spent_scopes(fake_redis) == {"staff_login_device"}

        code = await _rejected(tenant.id, staff_login, PASSWORD, client_ip=ip, device_token=device)
        assert code == ERR_AUTH_LOGIN_RATE_LIMITED
        # Ключ учётки не тронут: другой браузер входит.
        assert await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())
    finally:
        get_settings.cache_clear()


async def test_device_is_known_only_to_logins_that_entered_on_it(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Токен выдан после верного пароля одному логину: чужую учётку с него не
    подобрать, `BORM` соседнего отеля — тоже чужая учётка."""
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
    await create_staff_user("BORM", tenant_id=tenant.id, role=StaffRole.STAFF)
    await create_staff_user("BORM", tenant_id=other.id, role=StaffRole.STAFF)
    colleague = unique_login()
    await create_staff_user(colleague, tenant_id=tenant.id, role=StaffRole.STAFF)
    _counting_redis(monkeypatch, account=1, ip=60)
    try:
        ip = _unique_ip()
        device = (await login(tenant.id, "BORM", PASSWORD, client_ip=ip)).device_token
        assert await known_device_key(device, tenant.id, "BORM") is not None
        assert await known_device_key(device, tenant.id, colleague) is None
        assert await known_device_key(device, other.id, "BORM") is None

        await _rejected(tenant.id, colleague, WRONG, client_ip=ip)
        code = await _rejected(tenant.id, colleague, PASSWORD, client_ip=ip, device_token=device)
        assert code == ERR_AUTH_LOGIN_RATE_LIMITED
    finally:
        get_settings.cache_clear()


async def test_shared_computer_keeps_one_token_for_every_login(tenant: Tenant) -> None:
    """Общий компьютер стойки: второй сотрудник входит с тем же токеном, и
    браузер знаком обоим — у каждого своя пара и свой ключ бюджета."""
    first, second = unique_login(), unique_login()
    for staff_login in (first, second):
        await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    ip = _unique_ip()

    device = (await login(tenant.id, first, PASSWORD, client_ip=ip)).device_token
    again = await login(tenant.id, second, PASSWORD, client_ip=ip, device_token=device)
    assert again.device_token == device
    await login(tenant.id, first, PASSWORD, client_ip=ip, device_token=device)  # продление

    keys = {
        await known_device_key(device, tenant.id, staff_login) for staff_login in (first, second)
    }
    assert None not in keys
    assert len(keys) == 2
    async with platform_session_scope() as session:
        rows = await session.scalar(select(func.count()).select_from(StaffKnownDevice))
    assert rows == 2


@pytest.mark.parametrize("planted", ["garbage", "A" * 43])
async def test_foreign_token_is_replaced_not_trusted(tenant: Tenant, planted: str) -> None:
    """Значение не того вида и подброшенный токен, которого сервер не выдавал,
    входом знакомыми не становятся: браузер получает новый токен."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)

    grant = await login(
        tenant.id, staff_login, PASSWORD, client_ip=_unique_ip(), device_token=planted
    )
    assert grant.device_token != planted
    assert await known_device_key(planted, tenant.id, staff_login) is None
    assert await known_device_key(grant.device_token, tenant.id, staff_login) is not None


async def test_device_is_forgotten_after_ttl_without_logins(tenant: Tenant) -> None:
    """Знакомым браузер остаётся `KNOWN_DEVICE_TTL_DAYS` от последнего входа."""
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    device = (await login(tenant.id, staff_login, PASSWORD, client_ip=_unique_ip())).device_token
    async with platform_session_scope() as session:
        await session.execute(
            update(StaffKnownDevice).values(
                last_used_at=utc_now() - timedelta(days=KNOWN_DEVICE_TTL_DAYS, minutes=1)
            )
        )

    assert await known_device_key(device, tenant.id, staff_login) is None
