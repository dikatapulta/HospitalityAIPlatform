"""Подтверждение действия ключом устройства или паролем (ADR-008 §8, spec 0077 §4, §12 PR 2).

Верная подпись проходит, а чужой ключ, повтор вызова, вызов от другого хэша и снятый
флаг проверки владельца — нет; пароль — под бюджетами входа; погашенный ключ не
подтверждает новое, но прошлые подписи им проверяются.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime

import pytest
from webauthn.helpers import base64url_to_bytes

from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import deactivate_user
from hospitality.platform.staff_confirm import (
    ConfirmationMethod,
    ConfirmationProof,
    ConfirmationSubject,
    ProofStatus,
    _challenge,
    begin_confirmation,
    confirm_with_passkey,
    confirm_with_password,
    content_sha256,
    verify_proof,
)
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_RATE_LIMITED
from hospitality.platform.staff_passkey_binding import begin_binding, finish_binding
from hospitality.platform.staff_passkeys import ERR_AUTH_CONFIRMATION_FAILED, StaffPasskey
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from tests.conftest import FakeRateLimitRedis
from tests.passkey_authenticator import SoftAuthenticator, StaffMember, staff_member
from tests.test_staff_auth import PASSWORD

IP = "ip-confirm"


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


def _subject(tenant: Tenant, text: str = "Прошу закупить: 10 полотенец") -> ConfirmationSubject:
    return ConfirmationSubject(
        tenant_id=tenant.id,
        record_id=uuid.UUID(int=14),
        action="sign",
        content_sha256=content_sha256("Закупка", text),
    )


async def _signer(tenant: Tenant) -> tuple[StaffMember, SoftAuthenticator]:
    member = await staff_member(tenant.id, tenant.slug)
    device = SoftAuthenticator()
    options = await begin_binding(member.context, PASSWORD, client_ip=IP, device_token=None)
    await finish_binding(
        member.context, device.register(options), device_label="iPhone", notice_url="/"
    )
    return member, device


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


async def _reason(call: Awaitable[object]) -> str:
    with pytest.raises(AppError) as error:
        await call
    assert error.value.code == ERR_AUTH_CONFIRMATION_FAILED
    return str((error.value.details or [{}])[0].get("reason"))


async def test_passkey_proof_binds_content_and_signer(tenant: Tenant) -> None:
    """Подпись сходится с содержимым и подписантом; правка текста в базе её ломает."""
    member, device = await _signer(tenant)
    subject = _subject(tenant)
    proof = await _sign(member, device, subject)

    assert proof.method is ConfirmationMethod.PASSKEY
    stored = ConfirmationProof.model_validate_json(proof.model_dump_json())
    user_id = member.context.user_id
    assert await verify_proof(subject, stored, signer_user_id=user_id) is ProofStatus.VERIFIED
    edited = _subject(tenant, "Прошу закупить: 100 полотенец")
    assert await verify_proof(edited, stored, signer_user_id=user_id) is ProofStatus.MISMATCH
    other_user = uuid.uuid4()
    assert await verify_proof(subject, stored, signer_user_id=other_user) is ProofStatus.MISMATCH
    async with platform_session_scope() as session:
        passkey = await session.get(StaffPasskey, proof.passkey_id)
        assert passkey is not None
        assert passkey.sign_count == device.sign_count and passkey.last_used_at is not None


async def test_challenge_is_one_time(tenant: Tenant) -> None:
    """Повтор того же ответа — отказ: вызов погашен первым подтверждением."""
    member, device = await _signer(tenant)
    subject = _subject(tenant)
    request = await begin_confirmation(member.context, subject)
    answer = device.sign(request.options_json)
    await confirm_with_passkey(
        member.context, subject, challenge_id=request.challenge_id, credential_json=answer
    )
    reason = await _reason(
        confirm_with_passkey(
            member.context, subject, challenge_id=request.challenge_id, credential_json=answer
        )
    )
    assert reason == "expired"


async def test_old_answer_does_not_fit_new_challenge(tenant: Tenant) -> None:
    member, device = await _signer(tenant)
    subject = _subject(tenant)
    first = await begin_confirmation(member.context, subject)
    old_answer = device.sign(first.options_json)
    second = await begin_confirmation(member.context, subject)
    reason = await _reason(
        confirm_with_passkey(
            member.context, subject, challenge_id=second.challenge_id, credential_json=old_answer
        )
    )
    assert reason == "invalid"


async def test_challenge_of_other_content_is_refused(tenant: Tenant) -> None:
    """Вызов, выданный под один текст, не подтверждает другой — ни сам, ни подписью чужого хэша."""
    member, device = await _signer(tenant)
    shown, sent = _subject(tenant), _subject(tenant, "Прошу закупить: 100 полотенец")
    request = await begin_confirmation(member.context, shown)
    reason = await _reason(
        confirm_with_passkey(
            member.context,
            sent,
            challenge_id=request.challenge_id,
            credential_json=device.sign(request.options_json),
        )
    )
    assert reason == "expired"

    request = await begin_confirmation(member.context, shown)
    nonce = base64url_to_bytes(request.challenge_id)
    forged = device.sign(None, challenge=_challenge(sent, member.context.user_id, nonce))
    reason = await _reason(
        confirm_with_passkey(
            member.context, shown, challenge_id=request.challenge_id, credential_json=forged
        )
    )
    assert reason == "invalid"


async def test_foreign_and_unverified_keys_are_refused(tenant: Tenant) -> None:
    member, device = await _signer(tenant)
    colleague, colleague_device = await _signer(tenant)
    subject = _subject(tenant)

    request = await begin_confirmation(member.context, subject)
    reason = await _reason(
        confirm_with_passkey(
            member.context,
            subject,
            challenge_id=request.challenge_id,
            credential_json=colleague_device.sign(request.options_json),
        )
    )
    assert reason == "unknown_key"

    device.user_verified = False  # устройство не проверило владельца
    request = await begin_confirmation(member.context, subject)
    reason = await _reason(
        confirm_with_passkey(
            member.context,
            subject,
            challenge_id=request.challenge_id,
            credential_json=device.sign(request.options_json),
        )
    )
    assert reason == "invalid"


async def test_cloned_key_counter_is_refused(tenant: Tenant) -> None:
    """Счётчик подписей не вырос — копия ключа: отказ."""
    member, device = await _signer(tenant)
    subject = _subject(tenant)
    await _sign(member, device, subject)
    device.sign_count -= 1
    request = await begin_confirmation(member.context, subject)
    reason = await _reason(
        confirm_with_passkey(
            member.context,
            subject,
            challenge_id=request.challenge_id,
            credential_json=device.sign(request.options_json),
        )
    )
    assert reason == "invalid"


async def test_other_session_cannot_finish(tenant: Tenant) -> None:
    member, device = await _signer(tenant)
    other = await staff_member(tenant.id, tenant.slug, staff_login=member.login)
    subject = _subject(tenant)
    request = await begin_confirmation(member.context, subject)
    reason = await _reason(
        confirm_with_passkey(
            other.context,
            subject,
            challenge_id=request.challenge_id,
            credential_json=device.sign(request.options_json),
        )
    )
    assert reason == "expired"


async def test_foreign_tenant_subject_is_refused(tenant: Tenant) -> None:
    member, _ = await _signer(tenant)
    foreign = _subject(tenant).model_copy(update={"tenant_id": uuid.uuid4()})
    assert await _reason(begin_confirmation(member.context, foreign)) == "foreign_tenant"


async def test_password_confirmation_is_marked_as_password(
    tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пароль — запасной путь без ключа: доказательство честно говорит, что оно не криптография."""
    monkeypatch.setenv("STAFF_LOGIN_RATE_LIMIT_ATTEMPTS", "2")
    get_settings.cache_clear()
    try:
        member = await staff_member(tenant.id, tenant.slug, remember=False)
        subject = _subject(tenant)
        request = await begin_confirmation(member.context, subject)
        assert request.options_json is None  # ключей нет — окно предлагает только пароль

        reason = await _reason(
            confirm_with_password(
                member.context,
                subject,
                challenge_id=request.challenge_id,
                password="wrong-password!",
                client_ip=IP,
                device_token=None,
            )
        )
        assert reason == "password"
        # Ошибка пароля вызов не гасит — тот же вызов, верный пароль.
        proof = await confirm_with_password(
            member.context,
            subject,
            challenge_id=request.challenge_id,
            password=PASSWORD,
            client_ip=IP,
            device_token=None,
        )
        assert proof.method is ConfirmationMethod.PASSWORD and proof.passkey_id is None
        status = await verify_proof(subject, proof, signer_user_id=member.context.user_id)
        assert status is ProofStatus.PASSWORD_ONLY

        request = await begin_confirmation(member.context, subject)
        await _reason(
            confirm_with_password(
                member.context,
                subject,
                challenge_id=request.challenge_id,
                password="wrong-password!",
                client_ip=IP,
                device_token=None,
            )
        )
        with pytest.raises(AppError) as error:
            await confirm_with_password(
                member.context,
                subject,
                challenge_id=request.challenge_id,
                password=PASSWORD,
                client_ip=IP,
                device_token=None,
            )
        assert error.value.code == ERR_AUTH_LOGIN_RATE_LIMITED
    finally:
        get_settings.cache_clear()


async def test_revoked_key_cannot_sign_but_old_proof_verifies(tenant: Tenant) -> None:
    member, device = await _signer(tenant)
    manager = await staff_member(tenant.id, tenant.slug, role=StaffRole.MANAGER)
    subject = _subject(tenant)
    proof = await _sign(member, device, subject)
    request = await begin_confirmation(member.context, subject)
    assert request.options_json is not None

    await deactivate_user(member.context.user_id, actor_user_id=manager.context.user_id)
    reason = await _reason(
        confirm_with_passkey(
            member.context,
            subject,
            challenge_id=request.challenge_id,
            credential_json=device.sign(request.options_json),
        )
    )
    assert reason == "unknown_key"
    status = await verify_proof(subject, proof, signer_user_id=member.context.user_id)
    assert status is ProofStatus.VERIFIED


async def test_store_down_means_no_confirmation(
    tenant: Tenant, fake_redis: FakeRateLimitRedis
) -> None:
    """Redis недоступен — подтверждать нечем: 503, а не подпись без вызова."""
    member, _ = await _signer(tenant)
    fake_redis.fail = True
    with pytest.raises(AppError) as error:
        await begin_confirmation(member.context, _subject(tenant))
    assert (error.value.code, error.value.status_code) == (ERR_AUTH_CONFIRMATION_FAILED, 503)


def test_proof_fields_must_match_method() -> None:
    with pytest.raises(ValueError):
        ConfirmationProof(
            method=ConfirmationMethod.PASSWORD,
            nonce="n",
            confirmed_at=datetime.now(UTC),
            signature="s",
        )
    with pytest.raises(ValueError):
        ConfirmationProof(
            method=ConfirmationMethod.PASSKEY, nonce="n", confirmed_at=datetime.now(UTC)
        )


def test_content_hash_is_unambiguous() -> None:
    assert content_sha256("ab", "c") != content_sha256("a", "bc")
