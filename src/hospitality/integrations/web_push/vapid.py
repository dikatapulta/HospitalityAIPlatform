"""Адаптер веб-пуша со службами пуша браузеров — VAPID (RFC 8292) + RFC 8291.

Сообщение шифруется под ключи подписки (`encryption.py`), а запрос
подписывается JWT ES256 приватным ключом VAPID: служба пуша сверяет его с
публичным ключом, на который браузер подписывался, — чужой сервер с одним
адресом подписки сообщение не отправит. Своя реализация на `cryptography`, а не
`pywebpush`: тот тянет `aiohttp` и `requests`, а его основа — без типов (spec 0050 §5).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from hospitality.integrations.web_push.encryption import encrypt_payload
from hospitality.shared.ports.web_push import (
    MAX_PAYLOAD_BYTES,
    PushDelivery,
    PushOutcome,
    PushTarget,
    b64url_encode,
    validate_push_endpoint,
)

# Срок подписи: RFC 8292 §2 — не дольше суток; 12 ч, как у pywebpush, с запасом
# на расхождение часов сервера и службы пуша.
_TOKEN_LIFETIME_SECONDS: Final = 12 * 60 * 60
_TIMEOUT_SECONDS: Final = 10.0


def outcome_for_status(status_code: int) -> PushOutcome:
    """Исход по коду ответа службы пуша (spec 0050 §5)."""
    if 200 <= status_code < 300:
        return PushOutcome.DELIVERED
    if status_code in (404, 410):
        return PushOutcome.GONE
    if status_code == 429 or not 400 <= status_code < 500:
        return PushOutcome.FAILED
    return PushOutcome.REJECTED


class VapidWebPushAdapter:
    backend = "vapid"

    def __init__(
        self,
        private_key: bytes,
        subject: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._private_key = ec.derive_private_key(
            int.from_bytes(private_key, "big"), ec.SECP256R1()
        )
        public_key = self._private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.application_server_key = b64url_encode(public_key)
        self._subject = subject
        self._transport = transport
        self._clock = clock

    def authorization(self, endpoint: str) -> str:
        """Заголовок `Authorization: vapid t=<JWT>, k=<ключ>` для источника адреса."""
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int(self._clock()) + _TOKEN_LIFETIME_SECONDS,
            "sub": self._subject,
        }
        header = {"typ": "JWT", "alg": "ES256"}
        signing_input = ".".join(
            b64url_encode(json.dumps(part, separators=(",", ":")).encode())
            for part in (header, claims)
        )
        der_signature = self._private_key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        signature = b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={signing_input}.{signature}, k={self.application_server_key}"

    async def send(self, target: PushTarget, payload: bytes, *, ttl_seconds: int) -> PushDelivery:
        validate_push_endpoint(target.endpoint)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"push payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        headers = {
            "Authorization": self.authorization(target.endpoint),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl_seconds),
            # Обычная срочность Android в режиме сна откладывает доставку на минуты.
            "Urgency": "high",
        }
        body = encrypt_payload(target, payload)
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                response = await client.post(target.endpoint, content=body, headers=headers)
        except httpx.HTTPError:
            return PushDelivery(PushOutcome.FAILED)
        return PushDelivery(outcome_for_status(response.status_code), response.status_code)

    async def check(self) -> bool:
        return True
