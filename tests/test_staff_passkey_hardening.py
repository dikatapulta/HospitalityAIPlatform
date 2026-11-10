"""Ключи подписи до экранов кабинета: находки аудита #949 (В2-3 … В2-9) и ревью PR #943.

Отвергнутый «Это не я» ключ не даёт «подтверждено с личного устройства»; «Это не я» —
с паролем и только по открытой плашке, гасит все сессии, кроме текущей; новый пароль
по ссылке гасит ключи; сессии суперадмина привязки нет; название устройства — из
закрытого словаря; подтверждение ключом сверяет источник и идентификатор сайта.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable

import pytest

from hospitality.platform.models import StaffRole, StaffSession, Tenant
from hospitality.platform.staff_confirm import (
    ConfirmationProof,
    ConfirmationSubject,
    ProofStatus,
    begin_confirmation,
    confirm_with_passkey,
    content_sha256,
    verify_proof,
)
from hospitality.platform.staff_passkey_binding import (
    ERR_AUTH_PASSKEY_BINDING,
    begin_binding,
    device_label,
    finish_binding,
)
from hospitality.platform.staff_passkey_notice import (
    acknowledge_binding,
    binding_notice_text,
    disown_binding,
    unseen_bindings,
)
from hospitality.platform.staff_passkeys import (
    ERR_AUTH_CONFIRMATION_FAILED,
    PasskeyInfo,
    PasskeyRevokeReason,
    StaffPasskey,
    list_passkeys,
)
from hospitality.platform.staff_password_reset import accept_password_reset
from hospitality.platform.staff_superadmin import issue_password_reset
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from tests.conftest import FakeRateLimitRedis
from tests.passkey_authenticator import (
    IPHONE_SAFARI_UA,
    SoftAuthenticator,
    StaffMember,
    staff_member,
)
from tests.test_staff_auth import PASSWORD

IP = "ip-hardening"


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRateLimitRedis:
    fake = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    return fake


@pytest.fixture
async def tenant(canonical_database: None, fake_redis: FakeRateLimitRedis) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-a", name="Hotel A")
        session.add(row)
        await session.flush()
        return row


async def _bind(member: StaffMember, device: SoftAuthenticator) -> PasskeyInfo:
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    return await finish_binding(
        member.context, device.register(options), user_agent=IPHONE_SAFARI_UA, notice_url="/"
    )


def _subject(tenant: Tenant) -> ConfirmationSubject:
    return ConfirmationSubject(
        tenant_id=tenant.id,
        record_id=uuid.UUID(int=14),
        action="sign",
        content_sha256=content_sha256("Закупка", "Прошу закупить: 10 полотенец"),
    )


async def _sign(
    member: StaffMember, device: SoftAuthenticator, subject: ConfirmationSubject
) -> ConfirmationProof:
    request = await begin_confirmation(member.context, subject)
    return await confirm_with_passkey(
        member.context,
        subject,
        challenge_id=request.challenge_id,
        credential_json=device.sign(request.options_json),
    )


async def _refusal(call: Awaitable[object]) -> tuple[str, str]:
    with pytest.raises(AppError) as error:
        await call
    return error.value.code, str((error.value.details or [{}])[0].get("reason"))


async def _disown(member: StaffMember, passkey_id: uuid.UUID) -> bool:
    return await disown_binding(
        member.context, passkey_id, PASSWORD, client_ip=IP, device_token=None
    )


async def _revoked_reason(passkey_id: uuid.UUID) -> str | None:
    async with platform_session_scope() as session:
        row = await session.get(StaffPasskey, passkey_id)
        assert row is not None
        return row.revoked_reason


async def _session_alive(session_id: uuid.UUID) -> bool:
    async with platform_session_scope() as session:
        row = await session.get(StaffSession, session_id)
        assert row is not None
        return row.revoked_at is None


async def test_signature_of_disowned_key_is_not_verified(tenant: Tenant) -> None:
    """В2-3: подпись ключом, который владелец назвал чужим, — `DISOWNED`, а не `VERIFIED`."""
    phone = await staff_member(tenant.id, tenant.slug)
    device = SoftAuthenticator()
    info = await _bind(phone, device)
    subject = _subject(tenant)
    proof = await _sign(phone, device, subject)
    user_id = phone.context.user_id
    assert await verify_proof(subject, proof, signer_user_id=user_id) is ProofStatus.VERIFIED

    owner = await staff_member(tenant.id, tenant.slug, staff_login=phone.login)
    assert await _disown(owner, info.id)
    assert await verify_proof(subject, proof, signer_user_id=user_id) is ProofStatus.DISOWNED


async def test_disown_needs_password(tenant: Tenant) -> None:
    """В2-4: «Это не я» — отвязка, а отвязка — с паролем (ADR-008 §8)."""
    phone = await staff_member(tenant.id, tenant.slug)
    info = await _bind(phone, SoftAuthenticator())
    owner = await staff_member(tenant.id, tenant.slug, staff_login=phone.login)
    code, reason = await _refusal(
        disown_binding(owner.context, info.id, "wrong-password!", client_ip=IP, device_token=None)
    )
    assert (code, reason) == (ERR_AUTH_CONFIRMATION_FAILED, "password")
    assert [key.id for key in await list_passkeys(phone.context.user_id)] == [info.id]


async def test_disown_only_while_banner_is_open_and_not_from_binding_session(
    tenant: Tenant,
) -> None:
    """В2-4: привязавшая сессия и сессия после «Это я» ключ «Это не я» не гасят —
    иначе владелец отрёкся бы от устройства, которым уже подписывал."""
    phone = await staff_member(tenant.id, tenant.slug)
    info = await _bind(phone, SoftAuthenticator())
    assert not await _disown(phone, info.id)

    owner = await staff_member(tenant.id, tenant.slug, staff_login=phone.login)
    assert await acknowledge_binding(owner.context, info.id)
    later = await staff_member(tenant.id, tenant.slug, staff_login=phone.login)
    assert not await _disown(later, info.id)
    assert not await _disown(owner, info.id)
    assert [key.id for key in await list_passkeys(phone.context.user_id)] == [info.id]


async def test_disown_revokes_every_other_session(tenant: Tenant) -> None:
    """Ревью PR #943: знающий пароль мог войти не один раз — гаснут все сессии, кроме этой."""
    phone = await staff_member(tenant.id, tenant.slug)
    info = await _bind(phone, SoftAuthenticator())
    second = await staff_member(tenant.id, tenant.slug, staff_login=phone.login)
    owner = await staff_member(tenant.id, tenant.slug, staff_login=phone.login)
    assert [key.id for key in await unseen_bindings(owner.context)] == [info.id]

    assert await _disown(owner, info.id)
    assert await _revoked_reason(info.id) == PasskeyRevokeReason.DISOWNED
    assert not await _session_alive(phone.context.session_id)
    assert not await _session_alive(second.context.session_id)
    assert await _session_alive(owner.context.session_id)
    assert await unseen_bindings(owner.context) == []


async def test_new_password_revokes_passkeys(tenant: Tenant) -> None:
    """В2-5: новый пароль по ссылке гасит и ключи — их мог привязать знавший старый;
    прошлые подписи остаются проверяемыми."""
    manager = await staff_member(tenant.id, tenant.slug, role=StaffRole.MANAGER)
    phone = await staff_member(tenant.id, tenant.slug)
    device = SoftAuthenticator()
    info = await _bind(phone, device)
    subject = _subject(tenant)
    proof = await _sign(phone, device, subject)

    reset = await issue_password_reset(
        phone.context.user_id, tenant.id, actor_user_id=manager.context.user_id
    )
    await accept_password_reset(reset.reset_token, password="new-horse-battery-staple")
    assert await list_passkeys(phone.context.user_id) == []
    assert await _revoked_reason(info.id) == PasskeyRevokeReason.PASSWORD_RESET
    status = await verify_proof(subject, proof, signer_user_id=phone.context.user_id)
    assert status is ProofStatus.VERIFIED


async def test_superadmin_session_cannot_bind(tenant: Tenant) -> None:
    """В2-9: сессия суперадмина запомнена всегда, не галочкой — личным устройством она
    не становится."""
    member = await staff_member(tenant.id, tenant.slug, role=StaffRole.MANAGER)
    superadmin = member.context.model_copy(update={"is_platform_admin": True})
    code, reason = await _refusal(
        begin_binding(superadmin, PASSWORD, client_ip=IP, device_token=None)
    )
    assert (code, reason) == (ERR_AUTH_PASSKEY_BINDING, "platform_admin")


@pytest.mark.parametrize(
    ("user_agent", "label"),
    [
        (IPHONE_SAFARI_UA, "iPhone · Safari"),
        (
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
            "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
            "Android · Samsung Internet",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
            "Windows · Edge",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) CriOS/130.0 Mobile/15E148 Safari/604.1",
            "iPhone · Chrome",
        ),
        ("Это я, всё в порядке — не нажимайте «Это не я»", "Устройство"),
        ("", "Устройство"),
    ],
)
def test_device_label_comes_from_closed_dictionary(user_agent: str, label: str) -> None:
    """В2-7: название в предупреждении владельцу задаёт не привязавший, а словарь сервера."""
    assert device_label(user_agent) == label


async def test_notice_quotes_server_label(tenant: Tenant) -> None:
    """В2-7: в тексте предупреждения — название из словаря, в кавычках."""
    phone = await staff_member(tenant.id, tenant.slug)
    options = await begin_binding(phone.context, PASSWORD, client_ip=IP, device_token=None)
    info = await finish_binding(
        phone.context,
        SoftAuthenticator().register(options),
        user_agent="Всё хорошо, это я сам — iPhone",
        notice_url="/",
    )
    assert info.device_label == "iPhone"
    text = binding_notice_text(info.device_label, "10.11.2026 23:50")
    assert "«iPhone», 10.11.2026 23:50" in text and "Всё хорошо" not in text


@pytest.mark.parametrize("tamper", [{"origin": "https://evil.example"}, {"rp_id": "evil.example"}])
async def test_passkey_confirmation_checks_origin_and_rp_id(
    tenant: Tenant, tamper: dict[str, str]
) -> None:
    """Аудит #949: сверку источника при подтверждении не держал ни один тест."""
    member = await staff_member(tenant.id, tenant.slug)
    device = SoftAuthenticator()
    await _bind(member, device)
    for field, value in tamper.items():
        setattr(device, field, value)
    subject = _subject(tenant)
    request = await begin_confirmation(member.context, subject)
    code, reason = await _refusal(
        confirm_with_passkey(
            member.context,
            subject,
            challenge_id=request.challenge_id,
            credential_json=device.sign(request.options_json),
        )
    )
    assert (code, reason) == (ERR_AUTH_CONFIRMATION_FAILED, "invalid")
