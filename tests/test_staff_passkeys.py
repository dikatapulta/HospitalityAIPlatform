"""Привязка устройства подписи и уведомление владельцу (ADR-008 §8, spec 0077 §4, §12 PR 2).

Ответы браузера — программный аутентификатор (`tests/passkey_authenticator.py`),
проверка — настоящая `webauthn`. Redis церемоний и лимитов — `FakeRateLimitRedis`
(канон 0023), пуш — Fake-адаптер порта.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Awaitable, Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hospitality.integrations.web_push import FakeWebPushAdapter
from hospitality.platform import staff_passkeys, staff_push
from hospitality.platform.models import StaffRole, StaffSession, Tenant
from hospitality.platform.staff_auth import deactivate_user
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.platform.staff_passkey_binding import (
    ERR_AUTH_PASSKEY_BINDING,
    begin_binding,
    device_label,
    finish_binding,
)
from hospitality.platform.staff_passkey_notice import (
    BOUND_PUSH_TITLE,
    acknowledge_binding,
    disown_binding,
    unseen_bindings,
)
from hospitality.platform.staff_passkeys import (
    ERR_AUTH_CONFIRMATION_FAILED,
    MAX_PASSKEYS_PER_USER,
    PasskeyRevokeReason,
    StaffPasskey,
    list_passkeys,
    revoke_own_passkey,
)
from hospitality.platform.staff_superadmin import erase_member
from hospitality.platform.staff_team import reset_member_passkeys
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.ports.web_push import b64url_encode, set_web_push, web_push_or_none
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.passkey_authenticator import (
    IPHONE_SAFARI_UA,
    SoftAuthenticator,
    StaffMember,
    staff_member,
)
from tests.test_staff_auth import PASSWORD
from tests.test_staff_superadmin import create_superadmin

IP = "ip-binding"
NOTICE_URL = "/staff/hotel-a/signature"


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRateLimitRedis:
    fake = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    return fake


@pytest.fixture
def fake_push() -> Iterator[FakeWebPushAdapter]:
    previous = web_push_or_none()
    adapter = FakeWebPushAdapter()
    set_web_push(adapter)
    yield adapter
    set_web_push(previous)


@pytest.fixture
async def tenant(canonical_database: None, fake_redis: FakeRateLimitRedis) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-a", name="Hotel A")
        session.add(row)
        await session.flush()
        return row


async def _member(tenant: Tenant, *, remember: bool = True) -> StaffMember:
    return await staff_member(tenant.id, tenant.slug, remember=remember)


async def _bind(member: StaffMember, device: SoftAuthenticator) -> staff_passkeys.PasskeyInfo:
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    return await finish_binding(
        member.context,
        device.register(options),
        user_agent=IPHONE_SAFARI_UA,
        notice_url=NOTICE_URL,
    )


async def _refusal(call: Awaitable[object]) -> tuple[str, str]:
    with pytest.raises(AppError) as error:
        await call
    details = error.value.details or [{}]
    return error.value.code, str(details[0].get("reason"))


async def _phone(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    endpoint = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"
    with tenant_context(tenant_id):
        await staff_push.save_subscription(
            user_id, endpoint, b64url_encode(public), b64url_encode(os.urandom(16))
        )
    return endpoint


async def _row(passkey_id: uuid.UUID) -> StaffPasskey:
    async with platform_session_scope() as session:
        row = await session.get(StaffPasskey, passkey_id)
        assert row is not None
        return row


async def test_binding_stores_key_and_tells_owner(
    tenant: Tenant, fake_push: FakeWebPushAdapter
) -> None:
    """Верная привязка пишет ключ и шлёт пуш на подписки владельца — со временем и устройством."""
    member = await _member(tenant)
    endpoint = await _phone(tenant.id, member.context.user_id)
    info = await _bind(member, SoftAuthenticator())

    assert [key.id for key in await list_passkeys(member.context.user_id)] == [info.id]
    assert info.device_label == "iPhone · Safari"
    [push] = [sent for sent in fake_push.sent if sent.endpoint == endpoint]
    payload = json.loads(push.payload)
    assert payload["title"] == BOUND_PUSH_TITLE
    assert "iPhone · Safari" in payload["body"] and "Это не я" in payload["body"]
    assert payload["url"] == NOTICE_URL


async def test_binding_needs_remembered_session(tenant: Tenant) -> None:
    """Короткая сессия — компьютер стойки (spec 0073): своим устройством он не становится."""
    member = await _member(tenant, remember=False)
    code, reason = await _refusal(
        begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "not_remembered")


async def test_binding_needs_password_and_spends_login_budget(
    tenant: Tenant, fake_redis: FakeRateLimitRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторный пароль — под бюджетами входа: неверный тратит попытку, исчерпанный — 429."""
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        member = await _member(tenant)
        for _ in range(2):
            code, reason = await _refusal(
                begin_binding(member.context, "wrong-password!", client_ip=IP, device_token=None)
            )
            assert (code, reason) == (ERR_AUTH_CONFIRMATION_FAILED, "password")
        assert any(key.startswith("ratelimit:staff_login_account") for key in fake_redis.counters)
        with pytest.raises(AppError) as error:
            await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
    finally:
        get_settings.cache_clear()


async def test_binding_answer_is_one_time_and_bound_to_session(tenant: Tenant) -> None:
    """Ответ браузера принимается один раз и только сессией, которая начинала."""
    member = await _member(tenant)
    other_session = await staff_member(tenant.id, tenant.slug, staff_login=member.login)
    device = SoftAuthenticator()
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    answer = device.register(options)

    code, reason = await _refusal(
        finish_binding(other_session.context, answer, user_agent="x", notice_url=NOTICE_URL)
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "expired")
    # Чужая сессия погасила церемонию — начинать заново.
    code, reason = await _refusal(
        finish_binding(member.context, answer, user_agent="x", notice_url=NOTICE_URL)
    )
    assert reason == "expired"
    assert await list_passkeys(member.context.user_id) == []


async def test_binding_expires(tenant: Tenant, monkeypatch: pytest.MonkeyPatch) -> None:
    member = await _member(tenant)
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    later = staff_passkeys._now() + staff_passkeys.CEREMONY_TTL_SECONDS + 1
    monkeypatch.setattr(staff_passkeys, "_now", lambda: later)
    code, reason = await _refusal(
        finish_binding(
            member.context,
            SoftAuthenticator().register(options),
            user_agent="x",
            notice_url=NOTICE_URL,
        )
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "expired")


@pytest.mark.parametrize(
    "device",
    [
        SoftAuthenticator(user_verified=False),  # устройство не проверило владельца
        SoftAuthenticator(origin="https://evil.example"),  # чужой сайт
        SoftAuthenticator(rp_id="evil.example"),
    ],
)
async def test_binding_rejects_unverified_answers(
    tenant: Tenant, device: SoftAuthenticator
) -> None:
    member = await _member(tenant)
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    code, reason = await _refusal(
        finish_binding(
            member.context, device.register(options), user_agent="x", notice_url=NOTICE_URL
        )
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "invalid")


async def test_binding_limit_and_same_key_twice(tenant: Tenant) -> None:
    member = await _member(tenant)
    device = SoftAuthenticator()
    await _bind(member, device)
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    code, reason = await _refusal(
        finish_binding(
            member.context, device.register(options), user_agent="x", notice_url=NOTICE_URL
        )
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "already_bound")

    for _ in range(MAX_PASSKEYS_PER_USER - 1):
        await _bind(member, SoftAuthenticator())
    code, reason = await _refusal(
        begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "limit")


async def test_banner_is_for_other_sessions_and_disown_revokes(tenant: Tenant) -> None:
    """Плашку видит и закрывает другая сессия владельца; «Это не я» гасит ключ и ту сессию."""
    member = await _member(tenant)
    info = await _bind(member, SoftAuthenticator())
    assert await unseen_bindings(member.context) == []
    assert not await acknowledge_binding(member.context, info.id)

    owner = await staff_member(tenant.id, tenant.slug, staff_login=member.login)
    assert [key.id for key in await unseen_bindings(owner.context)] == [info.id]
    assert await disown_binding(owner.context, info.id, PASSWORD, client_ip=IP, device_token=None)

    row = await _row(info.id)
    assert row.revoked_reason == PasskeyRevokeReason.DISOWNED
    assert await list_passkeys(member.context.user_id) == []
    async with platform_session_scope() as session:
        bound = await session.get(StaffSession, member.context.session_id)
        assert bound is not None and bound.revoked_at is not None
    assert await unseen_bindings(owner.context) == []


async def test_acknowledge_hides_banner(tenant: Tenant) -> None:
    member = await _member(tenant)
    info = await _bind(member, SoftAuthenticator())
    owner = await staff_member(tenant.id, tenant.slug, staff_login=member.login)
    assert await acknowledge_binding(owner.context, info.id)
    assert await unseen_bindings(owner.context) == []
    assert [key.id for key in await list_passkeys(member.context.user_id)] == [info.id]


async def test_self_revoke_needs_password(tenant: Tenant) -> None:
    member = await _member(tenant)
    info = await _bind(member, SoftAuthenticator())
    code, reason = await _refusal(
        revoke_own_passkey(
            member.context, info.id, "wrong-password!", client_ip=IP, device_token=None
        )
    )
    assert (code, reason) == (ERR_AUTH_CONFIRMATION_FAILED, "password")
    assert await revoke_own_passkey(
        member.context, info.id, PASSWORD, client_ip=IP, device_token=None
    )
    assert (await _row(info.id)).revoked_reason == PasskeyRevokeReason.SELF


async def test_deactivation_reset_and_erase_revoke_keys(tenant: Tenant) -> None:
    """Деактивация, «Сбросить подпись» и стирание гасят ключи пометкой — строка остаётся."""
    manager = await staff_member(tenant.id, tenant.slug, role=StaffRole.MANAGER)
    member = await _member(tenant)
    reset_key = await _bind(member, SoftAuthenticator())
    assert (
        await reset_member_passkeys(
            member.context.user_id, tenant.id, actor_user_id=manager.context.user_id
        )
        == 1
    )
    assert (await _row(reset_key.id)).revoked_reason == PasskeyRevokeReason.MANAGER

    deactivated_key = await _bind(member, SoftAuthenticator())
    await deactivate_user(member.context.user_id, actor_user_id=manager.context.user_id)
    assert (await _row(deactivated_key.id)).revoked_reason == PasskeyRevokeReason.DEACTIVATED

    erased = await _member(tenant)
    erased_key = await _bind(erased, SoftAuthenticator())
    superadmin_id, _ = await create_superadmin(tenant.id, "ROOTADM")
    await erase_member(erased.context.user_id, tenant.id, actor_user_id=superadmin_id)
    assert (await _row(erased_key.id)).revoked_reason == PasskeyRevokeReason.ERASED


async def test_reset_is_tenant_scoped(tenant: Tenant) -> None:
    member = await _member(tenant)
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
        other_id = other.id
    with pytest.raises(AppError):
        await reset_member_passkeys(member.context.user_id, other_id, actor_user_id=uuid.uuid4())


async def test_binding_survives_push_failure(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пуш не ушёл — ключ всё равно записан, плашка остаётся вторым путём."""
    member = await _member(tenant)
    await _phone(tenant.id, member.context.user_id)
    set_web_push(None)  # пуш выключен — `deliver` бросает
    info = await _bind(member, SoftAuthenticator())
    assert [key.id for key in await list_passkeys(member.context.user_id)] == [info.id]


def test_device_label_is_one_short_line() -> None:
    assert device_label(IPHONE_SAFARI_UA) == "iPhone · Safari"
    assert device_label("\t") == "Устройство"
