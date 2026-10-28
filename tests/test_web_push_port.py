"""Контрактные тесты порта веб-пуша (R-7, ADR-007, spec 0050 §5).

Канон — `tests/test_file_storage_port.py`: один набор на ВСЕ адаптеры порта.
VAPID-адаптер ходит в службу пуша через `httpx.MockTransport` — сети нет, а
заголовки, подпись и шифртекст проверяются настоящие. Шифрование сверено с
тестовым вектором RFC 8291 (приложение A) байт в байт; JWT — проверкой подписи
публичным ключом; тело — расшифровкой ключом «браузера».
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hospitality.integrations.web_push import (
    FakeWebPushAdapter,
    VapidWebPushAdapter,
    build_web_push,
)
from hospitality.integrations.web_push.encryption import encrypt_payload
from hospitality.integrations.web_push.vapid import outcome_for_status
from hospitality.shared.config import (
    InvalidStaffPushConfigError,
    Settings,
    staff_push_vapid_key_bytes,
)
from hospitality.shared.ports.web_push import (
    MAX_PAYLOAD_BYTES,
    InvalidPushSubscriptionError,
    PushOutcome,
    PushTarget,
    WebPushPort,
    b64url_decode,
    b64url_encode,
    build_push_target,
    validate_push_endpoint,
)
from hospitality.tools.generate_vapid_key import generate_vapid_key

ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-token"
_STATUS_FOR: dict[PushOutcome, int] = {
    PushOutcome.DELIVERED: 201,
    PushOutcome.GONE: 410,
    PushOutcome.REJECTED: 400,
    PushOutcome.FAILED: 503,
}


@dataclass
class Browser:
    """Браузер-подписчик: приватная пара к `p256dh` — чтобы расшифровать тело."""

    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    auth: bytes = field(default_factory=lambda: os.urandom(16))

    def target(self, endpoint: str = ENDPOINT) -> PushTarget:
        public = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return PushTarget(endpoint=endpoint, p256dh=public, auth=self.auth)

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8291 со стороны браузера: заголовок → ключ записи → открытый текст."""
        salt, key_length = body[:16], body[20]
        server_public = body[21 : 21 + key_length]
        ciphertext = body[21 + key_length :]
        browser_public = self.target().p256dh
        shared = self.private_key.exchange(
            ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_public)
        )
        key_info = b"WebPush: info\x00" + browser_public + server_public
        ikm = HKDF(hashes.SHA256(), 32, self.auth, key_info).derive(shared)
        cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
        record = AESGCM(cek).decrypt(nonce, ciphertext, None)
        assert record.endswith(b"\x02")
        return record[:-1]


class PushService:
    """Служба пуша для VAPID-адаптера: отвечает по сценарию и помнит запросы."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_by_endpoint: dict[str, int] = {}
        self.error: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_by_endpoint.get(str(request.url), 201))


@dataclass
class Harness:
    adapter: WebPushPort
    script: Callable[[str, PushOutcome], None]
    service: PushService | None = None


def _vapid(
    service: PushService, *, clock: Callable[[], float] | None = None
) -> VapidWebPushAdapter:
    private_key, _ = generate_vapid_key()
    return VapidWebPushAdapter(
        b64url_decode(private_key),
        "mailto:support@necturn.com",
        transport=httpx.MockTransport(service.handle),
        clock=clock or (lambda: 1_800_000_000.0),
    )


def _fake_harness() -> Harness:
    fake = FakeWebPushAdapter()
    return Harness(adapter=fake, script=fake.set_outcome)


def _vapid_harness() -> Harness:
    service = PushService()

    def script(endpoint: str, outcome: PushOutcome) -> None:
        service.status_by_endpoint[endpoint] = _STATUS_FOR[outcome]

    return Harness(adapter=_vapid(service), script=script, service=service)


ADAPTERS: dict[str, Callable[[], Harness]] = {"fake": _fake_harness, "vapid": _vapid_harness}


@pytest.fixture(params=sorted(ADAPTERS))
def harness(request: pytest.FixtureRequest) -> Harness:
    return ADAPTERS[request.param]()


# ---------- Контракт порта: оба адаптера ----------


def test_application_server_key_is_a_p256_point(harness: Harness) -> None:
    point = b64url_decode(harness.adapter.application_server_key)
    assert len(point) == 65
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)


async def test_send_delivers_by_default(harness: Harness) -> None:
    delivery = await harness.adapter.send(Browser().target(), b'{"title": "x"}', ttl_seconds=60)
    assert delivery.outcome is PushOutcome.DELIVERED


@pytest.mark.parametrize("outcome", list(PushOutcome))
async def test_send_reports_the_push_service_outcome(
    harness: Harness, outcome: PushOutcome
) -> None:
    harness.script(ENDPOINT, outcome)
    delivery = await harness.adapter.send(Browser().target(), b"{}", ttl_seconds=60)
    assert delivery.outcome is outcome


async def test_send_refuses_endpoint_outside_whitelist_before_network(harness: Harness) -> None:
    with pytest.raises(InvalidPushSubscriptionError):
        await harness.adapter.send(
            Browser().target("https://169.254.169.254/latest"), b"{}", ttl_seconds=60
        )
    if harness.service is not None:
        assert harness.service.requests == []


async def test_send_refuses_oversized_payload(harness: Harness) -> None:
    with pytest.raises(ValueError, match="payload"):
        await harness.adapter.send(
            Browser().target(), b"x" * (MAX_PAYLOAD_BYTES + 1), ttl_seconds=60
        )


async def test_check_is_healthy(harness: Harness) -> None:
    assert await harness.adapter.check() is True


async def test_fake_keeps_delivered_messages_only() -> None:
    fake = FakeWebPushAdapter()
    fake.set_outcome("https://web.push.apple.com/gone", PushOutcome.GONE)
    await fake.send(Browser().target(), b"one", ttl_seconds=30)
    await fake.send(Browser().target("https://web.push.apple.com/gone"), b"two", ttl_seconds=30)
    assert [(sent.endpoint, sent.payload, sent.ttl_seconds) for sent in fake.sent] == [
        (ENDPOINT, b"one", 30)
    ]


# ---------- VAPID: заголовки, подпись, шифртекст ----------


async def test_vapid_request_carries_encrypted_body_and_headers() -> None:
    service = PushService()
    adapter = _vapid(service)
    browser = Browser()
    await adapter.send(browser.target(), "Комната 305".encode(), ttl_seconds=1800)

    (request,) = service.requests
    assert str(request.url) == ENDPOINT
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["ttl"] == "1800"
    assert request.headers["urgency"] == "high"
    assert browser.decrypt(request.content) == "Комната 305".encode()
    assert "Комната".encode() not in request.content


async def test_vapid_jwt_is_signed_for_the_push_service_origin() -> None:
    service = PushService()
    adapter = _vapid(service, clock=lambda: 1_800_000_000.0)
    await adapter.send(Browser().target(), b"{}", ttl_seconds=60)

    scheme, _, params = service.requests[0].headers["authorization"].partition(" ")
    fields = dict(part.strip().split("=", 1) for part in params.split(","))
    assert scheme == "vapid"
    assert fields["k"] == adapter.application_server_key
    header_b64, claims_b64, signature_b64 = fields["t"].split(".")
    assert json.loads(b64url_decode(header_b64)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(b64url_decode(claims_b64)) == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_800_000_000 + 12 * 3600,
        "sub": "mailto:support@necturn.com",
    }
    raw_signature = b64url_decode(signature_b64)
    der = encode_dss_signature(
        int.from_bytes(raw_signature[:32], "big"), int.from_bytes(raw_signature[32:], "big")
    )
    public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b64url_decode(adapter.application_server_key)
    )
    public_key.verify(der, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(InvalidSignature):
        public_key.verify(der, b"forged", ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_vapid_network_failure_is_failed_without_status(error: Exception) -> None:
    service = PushService()
    service.error = error
    delivery = await _vapid(service).send(Browser().target(), b"{}", ttl_seconds=60)
    assert delivery.outcome is PushOutcome.FAILED
    assert delivery.status_code is None


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, PushOutcome.DELIVERED),
        (201, PushOutcome.DELIVERED),
        (404, PushOutcome.GONE),
        (410, PushOutcome.GONE),
        (400, PushOutcome.REJECTED),
        (403, PushOutcome.REJECTED),
        (413, PushOutcome.REJECTED),
        (429, PushOutcome.FAILED),
        (301, PushOutcome.FAILED),
        (500, PushOutcome.FAILED),
        (503, PushOutcome.FAILED),
    ],
)
def test_outcome_for_status(status: int, outcome: PushOutcome) -> None:
    assert outcome_for_status(status) is outcome


def test_encryption_matches_rfc8291_appendix_a() -> None:
    """RFC 8291, приложение A: те же ключи и соль — тот же шифртекст байт в байт."""
    server_key = ec.derive_private_key(
        int.from_bytes(b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    target = PushTarget(
        endpoint=ENDPOINT,
        p256dh=b64url_decode(
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
        ),
        auth=b64url_decode("BTBZMqHH6r4Tts7J_aSIgg"),
    )
    body = encrypt_payload(
        target,
        b"When I grow up, I want to be a watermelon",
        server_key=server_key,
        salt=b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    header = (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    ciphertext = "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    assert body == b64url_decode(header) + b64url_decode(ciphertext)


def test_fresh_key_and_salt_for_every_message() -> None:
    browser = Browser()
    first = encrypt_payload(browser.target(), b"same")
    second = encrypt_payload(browser.target(), b"same")
    assert first[:16] != second[:16] and first[21:86] != second[21:86]


# ---------- Белый список служб пуша и ключи подписки ----------


@pytest.mark.parametrize(
    "endpoint",
    [
        ENDPOINT,
        "https://web.push.apple.com/QGuQyavXutnMH",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
        "https://wns2-par02p.notify.windows.com/w/?token=BQYAAA",
    ],
)
def test_known_push_services_are_accepted(endpoint: str) -> None:
    assert validate_push_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com:8443/fcm/send/x",
        "https://user:pw@fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://evil.example/fcm.googleapis.com",
        "https://push.apple.com.evil.example/x",
        "https://127.0.0.1/x",
        "https://db:5432/",
        "https://fcm.googleapis.com/fcm/send/a b",
        "https://fcm.googleapis.com/" + "x" * 1024,
        "not a url",
    ],
)
def test_other_endpoints_are_rejected(endpoint: str) -> None:
    with pytest.raises(InvalidPushSubscriptionError):
        validate_push_endpoint(endpoint)


def test_build_push_target_decodes_browser_keys() -> None:
    browser = Browser()
    target = build_push_target(
        ENDPOINT, b64url_encode(browser.target().p256dh), b64url_encode(browser.auth)
    )
    assert target == browser.target()


@pytest.mark.parametrize(
    ("p256dh", "auth"),
    [
        ("not base64 !", b64url_encode(b"a" * 16)),
        (b64url_encode(b"\x04" + b"\x00" * 64), b64url_encode(b"a" * 16)),  # не точка кривой
        (b64url_encode(b"\x04" * 33), b64url_encode(b"a" * 16)),  # не та длина
        (None, b64url_encode(b"a" * 15)),  # секрет короче 16 байт
    ],
)
def test_build_push_target_rejects_malformed_keys(p256dh: str | None, auth: str) -> None:
    valid = b64url_encode(Browser().target().p256dh)
    with pytest.raises(InvalidPushSubscriptionError):
        build_push_target(ENDPOINT, p256dh if p256dh is not None else valid, auth)


# ---------- Настройка: ключ VAPID и выбор адаптера ----------


def _settings(**values: str) -> Settings:
    return Settings(**values)  # type: ignore[arg-type]


def test_generated_key_is_accepted_and_matches_adapter_key() -> None:
    private_key, public_key = generate_vapid_key()
    settings = _settings(staff_push_backend="vapid", staff_push_vapid_private_key=private_key)
    adapter = build_web_push(settings)
    assert isinstance(adapter, VapidWebPushAdapter)
    assert adapter.application_server_key == public_key


def test_vapid_without_key_means_push_is_off() -> None:
    assert build_web_push(_settings(staff_push_backend="vapid")) is None


def test_fake_backend_needs_no_key() -> None:
    assert isinstance(build_web_push(_settings(staff_push_backend="fake")), FakeWebPushAdapter)


@pytest.mark.parametrize(
    "bad_key",
    [
        "SECRETMARKER-not-base64!",
        b64url_encode(b"SECRETMARKER"),  # не 32 байта
        b64url_encode(b"\x00" * 32),  # ноль — не ключ
        b64url_encode(b"\xff" * 32),  # больше порядка кривой
    ],
)
def test_bad_vapid_key_is_rejected_without_printing_it(bad_key: str) -> None:
    settings = _settings(staff_push_backend="vapid", staff_push_vapid_private_key=bad_key)
    with pytest.raises(InvalidStaffPushConfigError) as rejected:
        staff_push_vapid_key_bytes(settings)
    assert bad_key not in str(rejected.value) and "SECRETMARKER" not in str(rejected.value)


def test_vapid_subject_must_be_mailto_or_https() -> None:
    with pytest.raises(InvalidStaffPushConfigError, match="SUBJECT"):
        staff_push_vapid_key_bytes(_settings(staff_push_vapid_subject="support@necturn.com"))
