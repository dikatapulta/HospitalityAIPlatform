"""Fake-адаптер веб-пуша (ADR-007, spec 0050): служба пуша в памяти процесса.

Тот же контракт и те же контрактные тесты, что у VAPID-адаптера. Ключ —
настоящая пара P-256, поэтому браузер на `make dev` подписывается и кнопки
работают; сообщения остаются в памяти (`sent`). Адрес, который «отписали»
(`set_outcome`), отвечает, как настоящая служба, — `gone`, `rejected`, `failed`.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hospitality.shared.ports.web_push import (
    MAX_PAYLOAD_BYTES,
    PushDelivery,
    PushOutcome,
    PushTarget,
    b64url_encode,
    validate_push_endpoint,
)

_STATUS_BY_OUTCOME: dict[PushOutcome, int | None] = {
    PushOutcome.DELIVERED: 201,
    PushOutcome.GONE: 410,
    PushOutcome.REJECTED: 400,
    PushOutcome.FAILED: None,
}


@dataclass(frozen=True)
class SentPush:
    endpoint: str
    payload: bytes
    ttl_seconds: int


class FakeWebPushAdapter:
    backend = "fake"

    def __init__(self) -> None:
        public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
        self.application_server_key = b64url_encode(
            public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        self.sent: list[SentPush] = []
        self._outcomes: dict[str, PushOutcome] = {}

    def set_outcome(self, endpoint: str, outcome: PushOutcome) -> None:
        """Как служба пуша будет отвечать на этот адрес дальше (тесты, демо)."""
        self._outcomes[endpoint] = outcome

    async def send(self, target: PushTarget, payload: bytes, *, ttl_seconds: int) -> PushDelivery:
        validate_push_endpoint(target.endpoint)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"push payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        outcome = self._outcomes.get(target.endpoint, PushOutcome.DELIVERED)
        if outcome is PushOutcome.DELIVERED:
            self.sent.append(SentPush(target.endpoint, bytes(payload), ttl_seconds))
        return PushDelivery(outcome, _STATUS_BY_OUTCOME[outcome])

    async def check(self) -> bool:
        return True
