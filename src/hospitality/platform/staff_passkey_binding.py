"""Привязка устройства подписи сотрудника (ADR-008 §8, spec 0077 §4).

Два шага: `begin_binding` отдаёт параметры для `navigator.credentials.create`,
браузер просит у владельца лицо, отпечаток или код блокировки и создаёт ключ,
`finish_binding` проверяет ответ (`webauthn`, своей криптографии нет) и пишет ключ
в `staff_passkeys`. Владельцу — пуш на все подписки и плашка при следующем входе
(`staff_passkey_notice`).

Привязка — только в запомненной сессии («Запомнить меня», spec 0073: галочка и
означает личное устройство) и с повторным вводом пароля. Иначе компьютер стойки
с общим ПИН-кодом Windows Hello стал бы «личным устройством» каждого, кто под
незапертой сессией владельца знает этот код (ревью PR #938). Ключ создаётся только
встроенным в устройство аутентификатором и с проверкой владельца (user verification).
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from webauthn import (
    generate_registration_options,
    options_to_json,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from hospitality.platform import staff_passkey_notice
from hospitality.platform.models import User
from hospitality.platform.staff_passkeys import (
    CEREMONY_TTL_SECONDS,
    DEVICE_LABEL_MAX_LENGTH,
    MAX_PASSKEYS_PER_USER,
    PasskeyInfo,
    StaffPasskey,
    active_passkeys,
    passkey_info,
    put_ceremony,
    relying_party,
    require_password,
    take_ceremony,
)
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_PASSKEY_BINDING: Final = "ERR-AUTH-018"

DEFAULT_DEVICE_LABEL: Final = "Устройство"
# Ответ браузера — сотни байт; больше — не ответ браузера, разбирать не стоит.
MAX_CREDENTIAL_JSON_LENGTH: Final = 16_384

_RP_NAME: Final = "Necturn"
_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f]+")


def _binding_refused(reason: str, *, status_code: int = 403) -> AppError:
    logger.warning("staff.passkey_binding_refused", reason=reason)
    return AppError(
        code=ERR_AUTH_PASSKEY_BINDING,
        message=f"Device binding refused: {reason}",
        status_code=status_code,
        details=[{"reason": reason}],
    )


def _binding_key(staff: StaffContext) -> str:
    return f"staff_passkey_bind:{staff.user_id}"


def _require_remembered(staff: StaffContext) -> None:
    # У запомненной сессии нет срока запирания (spec 0073; `resolve_staff_session`).
    if staff.session_locks_at is not None:
        raise _binding_refused("not_remembered")


async def begin_binding(
    staff: StaffContext, password: str, *, client_ip: str, device_token: str | None
) -> str:
    """Начать привязку этого устройства: параметры для `navigator.credentials.create` (JSON).

    Отказы: сессия не запомнена или ключей уже пять — ERR-AUTH-018; пароль не
    подошёл — ERR-AUTH-017, бюджет попыток исчерпан — ERR-AUTH-006; Redis
    недоступен — ERR-AUTH-018 (503). Новый вызов заменяет начатую привязку."""
    _require_remembered(staff)
    await require_password(staff, password, client_ip=client_ip, device_token=device_token)
    async with platform_session_scope() as session:
        keys = await active_passkeys(session, staff.user_id)
    if len(keys) >= MAX_PASSKEYS_PER_USER:
        raise _binding_refused("limit", status_code=409)
    rp = relying_party()
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=_RP_NAME,
        # В ключ на устройстве уходит только логин: имени сотрудника там нет (PII_REGISTRY).
        user_name=staff.login or str(staff.user_id),
        user_id=staff.user_id.bytes,
        challenge=challenge,
        timeout=CEREMONY_TTL_SECONDS * 1000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Ключ этого устройства, а не телефона рядом с чужим компьютером
            # (spec 0077, «Не входит»).
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=key.credential_id) for key in keys],
    )
    state = {"challenge": bytes_to_base64url(challenge), "session_id": str(staff.session_id)}
    if not await put_ceremony(_binding_key(staff), state):
        raise _binding_refused("unavailable", status_code=503)
    logger.info(
        "staff.passkey_binding_started",
        user_id=str(staff.user_id),
        session_id=str(staff.session_id),
    )
    return options_to_json(options)


def clean_device_label(raw: str) -> str:
    """Название устройства для листа подписей: одна строка, не длиннее колонки."""
    label = " ".join(_CONTROL_CHARS.sub(" ", raw).split())[:DEVICE_LABEL_MAX_LENGTH].strip()
    return label or DEFAULT_DEVICE_LABEL


async def finish_binding(
    staff: StaffContext, credential_json: str, *, device_label: str, notice_url: str
) -> PasskeyInfo:
    """Закончить привязку ответом браузера; владельцу — пуш на все подписки.

    `device_label` — название устройства для листа и плашки (из User-Agent, его
    собирает страница); `notice_url` — куда ведёт нажатие на пуш. Отказы —
    ERR-AUTH-018: сессия не запомнена, привязка истекла или начата другой
    сессией, ответ браузера не прошёл проверку, ключ уже привязан, ключей пять."""
    _require_remembered(staff)
    state = await take_ceremony(_binding_key(staff))
    if state is None or state.get("session_id") != str(staff.session_id):
        raise _binding_refused("expired")
    rp = relying_party()
    try:
        if len(credential_json) > MAX_CREDENTIAL_JSON_LENGTH:
            raise ValueError("credential is too long")
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(state["challenge"]),
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except Exception as exc:  # любой ответ, не прошедший разбор или проверку, — отказ
        logger.warning("staff.passkey_response_invalid", error_type=type(exc).__name__)
        raise _binding_refused("invalid") from exc
    async with platform_session_scope() as session:
        # Привязки одной учётки — по очереди: потолок в пять ключей держится под гонкой.
        await session.scalar(select(User.id).where(User.id == staff.user_id).with_for_update())
        taken = await session.scalar(
            select(StaffPasskey.id).where(StaffPasskey.credential_id == verified.credential_id)
        )
        if taken is not None:
            raise _binding_refused("already_bound", status_code=409)
        if len(await active_passkeys(session, staff.user_id)) >= MAX_PASSKEYS_PER_USER:
            raise _binding_refused("limit", status_code=409)
        passkey = StaffPasskey(
            user_id=staff.user_id,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            device_label=clean_device_label(device_label),
            bound_session_id=staff.session_id,
        )
        session.add(passkey)
        await session.flush()
        info = passkey_info(passkey)
    logger.info(
        "staff.passkey_bound",
        user_id=str(staff.user_id),
        passkey_id=str(info.id),
        session_id=str(staff.session_id),
        backed_up=verified.credential_backed_up,
    )
    await staff_passkey_notice.announce_binding(
        staff.user_id, staff.tenant_id, info.device_label, info.created_at, url=notice_url
    )
    return info
