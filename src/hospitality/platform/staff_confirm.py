"""Подтверждение действия сотрудника — ключом устройства или паролем (ADR-008 §8, spec 0077 §4).

Модуль, которому нужна подпись под действием, описывает подтверждаемое
`ConfirmationSubject` — отель, запись, действие и SHA-256 её неизменного содержимого
(`content_sha256`) — и проходит два шага: `begin_confirmation` → окно подтверждения
в браузере → `confirm_with_passkey` или `confirm_with_password`. Итог —
`ConfirmationProof`: модуль хранит его рядом со своей записью, а `verify_proof`
проверяет позже — содержимое в базе изменилось, и подпись ключа не сходится.

Вызов WebAuthn — SHA-256 от хэша подтверждаемого, учётки и одноразового случайного
числа сервера. Число живёт в Redis 5 минут, принадлежит сессии, начавшей
подтверждение, и гасится первым же ответом ключа: повтор того же ответа отказан.
Сервер проверяет подпись, источник, идентификатор сайта, флаг проверки владельца
(user verification — обязателен) и счётчик; проверку делает `webauthn`.

Пароль — запасной фактор: повторный ввод под бюджетами входа
(`staff_passkeys.require_password`). Его доказательство — запись «пароль введён
тогда-то», к содержимому криптографически не привязанная: `verify_proof` так и
отвечает — `PASSWORD_ONLY`, а не «сходится» (ревью PR #938). Пароль, заданный
по ссылке нового пароля, доказательство помечает её временем
(`password_reset_at`): ссылку выдаёт менеджер или суперадмин, и задать по ней
пароль мог тот, кто её держал (#944). Чем устройство
проверило владельца — лицом, отпечатком или кодом, — WebAuthn не сообщает, и
доказательство этого не утверждает.

В логах — учётка, id ключа, действие и id записи; ни содержимого, ни имён.
"""

from __future__ import annotations

import enum
import hashlib
import json
import secrets
import uuid
from typing import TYPE_CHECKING, Final, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from webauthn import generate_authentication_options, options_to_json
from webauthn import verify_authentication_response as _verify_assertion
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from hospitality.platform.staff_passkeys import (
    CEREMONY_TTL_SECONDS,
    StaffPasskey,
    active_passkeys,
    confirmation_refused,
    put_ceremony,
    relying_party,
    require_password,
    take_ceremony,
)
from hospitality.platform.staff_password_reset import password_set_by_link_at
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)

_SUBJECT_DOMAIN: Final = "necturn/confirm-subject/v1"
_CHALLENGE_DOMAIN: Final = b"necturn/staff-confirm/v1\n"
# Ответ браузера — сотни байт; больше — не ответ браузера, разбирать не стоит.
_MAX_CREDENTIAL_JSON_LENGTH: Final = 16_384


class ConfirmationMethod(enum.StrEnum):
    PASSKEY = "passkey"  # «подтверждено с личного устройства»
    PASSWORD = "password"  # «подтверждено паролем кабинета»


class ProofStatus(enum.StrEnum):
    VERIFIED = "verified"  # подпись ключа сходится с нынешним содержимым
    MISMATCH = "mismatch"  # не сходится: содержимое, подписант или само доказательство другие
    PASSWORD_ONLY = "password_only"  # пароль: проверять криптографически нечего


def content_sha256(*parts: str) -> str:
    """SHA-256 неизменного содержимого записи; части — JSON-массивом, склейка однозначна."""
    encoded = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class ConfirmationSubject(BaseModel):
    """Что подтверждается: отель, запись, действие и хэш её содержимого (spec 0077 §4)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    record_id: uuid.UUID
    action: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def digest(self) -> bytes:
        # Поля фиксированного формата без перевода строки — склейка однозначна.
        lines = (
            _SUBJECT_DOMAIN,
            str(self.tenant_id),
            str(self.record_id),
            self.action,
            self.content_sha256,
        )
        return hashlib.sha256("\n".join(lines).encode()).digest()


class ConfirmationRequest(BaseModel):
    """Окно подтверждения: id вызова и параметры для `navigator.credentials.get`.

    `options_json` — None, когда у сотрудника нет действующих ключей: окно
    предлагает только пароль."""

    challenge_id: str
    options_json: str | None


class ConfirmationProof(BaseModel):
    """Доказательство подтверждения; модуль-владелец хранит его рядом с записью (JSON).

    Байтовые поля — base64url. У способа `passkey` заполнены все поля ключа,
    у `password` — ни одного; `password_reset_at` — только у `password`: когда
    нынешний пароль задан по ссылке нового пароля (spec 0077 §4), иначе None."""

    model_config = ConfigDict(frozen=True)

    method: ConfirmationMethod
    nonce: str
    confirmed_at: AwareDatetime
    passkey_id: uuid.UUID | None = None
    credential_id: str | None = None
    authenticator_data: str | None = None
    client_data_json: str | None = None
    signature: str | None = None
    rp_id: str | None = None
    origin: str | None = None
    password_reset_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _fields_match_method(self) -> Self:
        passkey_fields = (
            self.passkey_id,
            self.credential_id,
            self.authenticator_data,
            self.client_data_json,
            self.signature,
            self.rp_id,
            self.origin,
        )
        if self.method is ConfirmationMethod.PASSKEY and None in passkey_fields:
            raise ValueError("passkey proof needs every passkey field")
        if self.method is ConfirmationMethod.PASSKEY and self.password_reset_at is not None:
            raise ValueError("passkey proof carries no password reset time")
        if self.method is ConfirmationMethod.PASSWORD and any(
            field is not None for field in passkey_fields
        ):
            raise ValueError("password proof carries no passkey fields")
        return self


def _challenge(subject: ConfirmationSubject, user_id: uuid.UUID, nonce: bytes) -> bytes:
    return hashlib.sha256(_CHALLENGE_DOMAIN + subject.digest() + user_id.bytes + nonce).digest()


def _ceremony_key(challenge_id: str) -> str:
    return f"staff_confirm:{challenge_id}"


def _require_own_tenant(staff: StaffContext, subject: ConfirmationSubject) -> None:
    # Подтверждать можно только запись отеля, в чьём кабинете сотрудник сейчас.
    if subject.tenant_id != staff.tenant_id:
        raise confirmation_refused("foreign_tenant")


async def begin_confirmation(
    staff: StaffContext, subject: ConfirmationSubject
) -> ConfirmationRequest:
    """Открыть окно подтверждения: одноразовый вызов на 5 минут для этой сессии.

    Redis недоступен — ERR-AUTH-017 (503): подтвердить нечем, действие не начинается."""
    _require_own_tenant(staff, subject)
    nonce = secrets.token_bytes(32)
    challenge_id = bytes_to_base64url(nonce)
    async with platform_session_scope() as session:
        keys = await active_passkeys(session, staff.user_id)
    options_json = None
    if keys:
        options = generate_authentication_options(
            rp_id=relying_party().rp_id,
            challenge=_challenge(subject, staff.user_id, nonce),
            timeout=CEREMONY_TTL_SECONDS * 1000,
            allow_credentials=[
                # Ключ встроен в устройство (привязка — `AuthenticatorAttachment.PLATFORM`):
                # браузер не предлагает телефон рядом с чужим компьютером.
                PublicKeyCredentialDescriptor(
                    id=key.credential_id, transports=[AuthenticatorTransport.INTERNAL]
                )
                for key in keys
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        options_json = options_to_json(options)
    state = {
        "user_id": str(staff.user_id),
        "session_id": str(staff.session_id),
        "subject": subject.digest().hex(),
    }
    if not await put_ceremony(_ceremony_key(challenge_id), state):
        raise confirmation_refused("unavailable", status_code=503)
    return ConfirmationRequest(challenge_id=challenge_id, options_json=options_json)


async def _take_challenge(
    staff: StaffContext, subject: ConfirmationSubject, challenge_id: str
) -> bytes:
    """Погасить вызов и вернуть его случайное число; чужой, истёкший, повторный — отказ."""
    state = await take_ceremony(_ceremony_key(challenge_id))
    if (
        state is None
        or state.get("user_id") != str(staff.user_id)
        or state.get("session_id") != str(staff.session_id)
        or state.get("subject") != subject.digest().hex()
    ):
        logger.warning("staff.confirmation_refused", reason="expired", user_id=str(staff.user_id))
        raise confirmation_refused("expired")
    return base64url_to_bytes(challenge_id)


async def confirm_with_passkey(
    staff: StaffContext,
    subject: ConfirmationSubject,
    *,
    challenge_id: str,
    credential_json: str,
) -> ConfirmationProof:
    """Подтверждение ключом устройства; отказ — ERR-AUTH-017.

    Вызов гасится и при отказе: попытка заново — новое окно подтверждения."""
    _require_own_tenant(staff, subject)
    nonce = await _take_challenge(staff, subject, challenge_id)
    try:
        if len(credential_json) > _MAX_CREDENTIAL_JSON_LENGTH:
            raise ValueError("credential is too long")
        credential = parse_authentication_credential_json(credential_json)
    except Exception as exc:
        logger.warning("staff.confirmation_refused", reason="invalid", user_id=str(staff.user_id))
        raise confirmation_refused("invalid") from exc
    rp = relying_party()
    async with platform_session_scope() as session:
        passkey = await session.scalar(
            select(StaffPasskey)
            .where(
                StaffPasskey.credential_id == credential.raw_id,
                StaffPasskey.user_id == staff.user_id,
                StaffPasskey.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if passkey is None:
            logger.warning(
                "staff.confirmation_refused", reason="unknown_key", user_id=str(staff.user_id)
            )
            raise confirmation_refused("unknown_key")
        try:
            verified = _verify_assertion(
                credential=credential,
                expected_challenge=_challenge(subject, staff.user_id, nonce),
                expected_rp_id=rp.rp_id,
                expected_origin=rp.origin,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            logger.warning(
                "staff.confirmation_refused",
                reason="invalid",
                user_id=str(staff.user_id),
                passkey_id=str(passkey.id),
                error_type=type(exc).__name__,
            )
            raise confirmation_refused("invalid") from exc
        now = utc_now()
        passkey.sign_count = verified.new_sign_count
        passkey.last_used_at = now
        proof = ConfirmationProof(
            method=ConfirmationMethod.PASSKEY,
            nonce=challenge_id,
            confirmed_at=now,
            passkey_id=passkey.id,
            credential_id=bytes_to_base64url(credential.raw_id),
            authenticator_data=bytes_to_base64url(credential.response.authenticator_data),
            client_data_json=bytes_to_base64url(credential.response.client_data_json),
            signature=bytes_to_base64url(credential.response.signature),
            rp_id=rp.rp_id,
            origin=rp.origin,
        )
    _log_confirmed(staff, subject, proof)
    return proof


async def confirm_with_password(
    staff: StaffContext,
    subject: ConfirmationSubject,
    *,
    challenge_id: str,
    password: str,
    client_ip: str,
    device_token: str | None,
) -> ConfirmationProof:
    """Подтверждение паролем кабинета — под бюджетами входа.

    Пароль проверяется до того, как гасится вызов: ошибся в пароле — вводишь
    снова в том же окне. Отказы: ERR-AUTH-017, исчерпан бюджет — ERR-AUTH-006."""
    _require_own_tenant(staff, subject)
    await require_password(staff, password, client_ip=client_ip, device_token=device_token)
    await _take_challenge(staff, subject, challenge_id)
    # Логина нет — `require_password` уже отказал.
    reset_at = (
        await password_set_by_link_at(staff.user_id, staff.tenant_id, staff.login)
        if staff.login is not None
        else None
    )
    proof = ConfirmationProof(
        method=ConfirmationMethod.PASSWORD,
        nonce=challenge_id,
        confirmed_at=utc_now(),
        password_reset_at=reset_at,
    )
    _log_confirmed(staff, subject, proof)
    return proof


def _log_confirmed(
    staff: StaffContext, subject: ConfirmationSubject, proof: ConfirmationProof
) -> None:
    logger.info(
        "staff.action_confirmed",
        user_id=str(staff.user_id),
        method=proof.method.value,
        passkey_id=str(proof.passkey_id) if proof.passkey_id else None,
        action=subject.action,
        record_id=str(subject.record_id),
    )


async def verify_proof(
    subject: ConfirmationSubject, proof: ConfirmationProof, *, signer_user_id: uuid.UUID
) -> ProofStatus:
    """Сходится ли сохранённое доказательство с нынешним содержимым и подписантом.

    Ключ ищется и среди погашенных: отвязанный телефон не делает прошлые подписи
    непроверяемыми. Счётчик здесь не проверяется — это не новое подтверждение."""
    if proof.method is ConfirmationMethod.PASSWORD:
        return ProofStatus.PASSWORD_ONLY
    async with platform_session_scope() as session:
        public_key = await session.scalar(
            select(StaffPasskey.public_key).where(
                StaffPasskey.id == proof.passkey_id,
                StaffPasskey.user_id == signer_user_id,
            )
        )
    # Поля ключа у способа `passkey` гарантирует валидатор доказательства.
    if public_key is None or proof.credential_id is None:
        return ProofStatus.MISMATCH
    try:
        _verify_assertion(
            credential=AuthenticationCredential(
                id=proof.credential_id,
                raw_id=base64url_to_bytes(proof.credential_id),
                response=AuthenticatorAssertionResponse(
                    client_data_json=base64url_to_bytes(proof.client_data_json or ""),
                    authenticator_data=base64url_to_bytes(proof.authenticator_data or ""),
                    signature=base64url_to_bytes(proof.signature or ""),
                ),
            ),
            expected_challenge=_challenge(subject, signer_user_id, base64url_to_bytes(proof.nonce)),
            expected_rp_id=proof.rp_id or "",
            expected_origin=proof.origin or "",
            credential_public_key=public_key,
            credential_current_sign_count=0,
            require_user_verification=True,
        )
    except Exception:
        return ProofStatus.MISMATCH
    return ProofStatus.VERIFIED
