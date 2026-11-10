"""Программный аутентификатор WebAuthn и сотрудник с сессией — для тестов подписи (spec 0077 §12).

Аутентификатор делает то же, что телефон с Face ID: ключ ES256 (P-256), аттестация
формата `none`, флаги «присутствие» и «проверка владельца», счётчик подписей.
Проверяет его ответы настоящая библиотека `webauthn` — тесты не подменяют
криптографию. Источник и идентификатор сайта — из `PUBLIC_BASE_URL` по умолчанию
(`http://localhost:8000`).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
import uuid
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor

from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import (
    StaffContext,
    load_staff_context,
    login,
    resolve_staff_session,
)
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

_USER_PRESENT = 0x01
_USER_VERIFIED = 0x04
_ATTESTED_DATA = 0x40


def _private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@dataclass
class SoftAuthenticator:
    """Ключ на «устройстве»: `register` — ответ на создание, `sign` — на подтверждение."""

    rp_id: str = "localhost"
    origin: str = "http://localhost:8000"
    user_verified: bool = True
    sign_count: int = 0
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    private_key: ec.EllipticCurvePrivateKey = field(default_factory=_private_key)

    def _cose_public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return encode_cbor(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, extra_flags: int = 0) -> bytes:
        flags = _USER_PRESENT | extra_flags | (_USER_VERIFIED if self.user_verified else 0)
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        return rp_hash + bytes([flags]) + struct.pack(">I", self.sign_count)

    def _client_data(self, kind: str, challenge: bytes) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": bytes_to_base64url(challenge),
                "origin": self.origin,
                "crossOrigin": False,
            }
        ).encode()

    def _credential(self, response: dict[str, str]) -> str:
        credential_id = bytes_to_base64url(self.credential_id)
        return json.dumps(
            {
                "id": credential_id,
                "rawId": credential_id,
                "type": "public-key",
                "response": response,
                "clientExtensionResults": {},
            }
        )

    def register(self, options_json: str) -> str:
        """Ответ `navigator.credentials.create` на параметры привязки."""
        challenge = base64url_to_bytes(json.loads(options_json)["challenge"])
        attested = (
            bytes(16)  # AAGUID: как у passkey Apple — нули
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_public_key()
        )
        attestation = encode_cbor(
            {"fmt": "none", "attStmt": {}, "authData": self._auth_data(_ATTESTED_DATA) + attested}
        )
        return self._credential(
            {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", challenge)
                ),
                "attestationObject": bytes_to_base64url(attestation),
            }
        )

    def sign(self, options_json: str | None, *, challenge: bytes | None = None) -> str:
        """Ответ `navigator.credentials.get`; `challenge` — подписать чужой вызов."""
        if challenge is None:
            assert options_json is not None
            challenge = base64url_to_bytes(json.loads(options_json)["challenge"])
        self.sign_count += 1
        auth_data = self._auth_data()
        client_data = self._client_data("webauthn.get", challenge)
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return self._credential(
            {
                "authenticatorData": bytes_to_base64url(auth_data),
                "clientDataJSON": bytes_to_base64url(client_data),
                "signature": bytes_to_base64url(signature),
            }
        )


@dataclass(frozen=True)
class StaffMember:
    """Сотрудник отеля с логином и его кабинет — контекст запроса, как у страницы."""

    login: str
    context: StaffContext


async def staff_member(
    tenant_id: uuid.UUID,
    tenant_slug: str,
    *,
    remember: bool = True,
    role: StaffRole = StaffRole.STAFF,
    staff_login: str | None = None,
) -> StaffMember:
    """Новый сотрудник (или новый вход существующего — `staff_login`) и его сессия."""
    if staff_login is None:
        staff_login = unique_login()
        await create_staff_user(staff_login, tenant_id=tenant_id, role=role)
    grant = await login(
        tenant_id, staff_login, PASSWORD, client_ip=f"ip-{uuid.uuid4().hex[:8]}", remember=remember
    )
    active = await resolve_staff_session(grant.session_token)
    assert active is not None
    context = await load_staff_context(active, tenant_slug)
    assert context is not None
    return StaffMember(login=staff_login, context=context)
