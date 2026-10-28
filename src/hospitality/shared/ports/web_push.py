"""Порт веб-пуша: уведомления персоналу на телефон (spec 0050, FOUNDATION §8, P-3).

Канон порта — `shared/ports/file_storage.py`: `typing.Protocol` здесь, адаптеры —
`integrations/web_push/` (Fake обязателен, ADR-007), общий контрактный набор
тестов, адаптер в порт кладёт только composition root (`set_web_push`).

Порт отправляет непрозрачные байты на адрес подписки браузера и говорит, чем
кончилось. О сотрудниках, тенантах и текстах не знает — это `platform/staff_push.py`.

Адрес подписки приходит от браузера, а запрос по нему шлёт сервер, поэтому
адрес проверяется здесь, до любого адаптера (`validate_push_endpoint`): только
`https://` на хостах служб пуша из белого списка — иначе сотрудник с доступом к
кабинету заставил бы сервер стучаться во внутреннюю сеть (SSRF).
"""

from __future__ import annotations

import base64
import binascii
import enum
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

# Хосты служб пуша (spec 0050 §4): Chrome и Android — Google, Safari и iPhone —
# Apple (webkit.org/blog/13878: «allow URLs from *.push.apple.com»), Firefox —
# Mozilla, Edge — Microsoft. Браузер вне списка получает отказ подписки.
_EXACT_HOSTS: Final = frozenset({"fcm.googleapis.com"})
_HOST_SUFFIXES: Final = (".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")
MAX_ENDPOINT_LENGTH: Final = 1024
# RFC 8291 §4: служба пуша обязана принять 4096 байт тела; за вычетом заголовка
# (86), разделителя и тега AES-GCM остаётся 3993 байта открытого текста.
MAX_PAYLOAD_BYTES: Final = 3993
_P256DH_LENGTH: Final = 65
_AUTH_LENGTH: Final = 16


class PushOutcome(enum.StrEnum):
    """Чем кончилась отправка — по ответу службы пуша."""

    DELIVERED = "delivered"  # 2xx: служба приняла сообщение
    GONE = "gone"  # 404/410: подписки больше нет — удалить
    REJECTED = "rejected"  # иной 4xx: сообщение или ключ отвергнуты — удалить и разбираться
    FAILED = "failed"  # 5xx, 429, таймаут, сеть: повторить позже


@dataclass(frozen=True)
class PushDelivery:
    outcome: PushOutcome
    # Код ответа службы пуша — для лога; у сетевого сбоя кода нет.
    status_code: int | None = None


@dataclass(frozen=True)
class PushTarget:
    """Подписка браузера: адрес службы пуша и ключи шифрования (RFC 8291)."""

    endpoint: str
    p256dh: bytes  # публичный ключ браузера, точка P-256 без сжатия (65 байт)
    auth: bytes  # секрет аутентификации браузера (16 байт)


class InvalidPushSubscriptionError(ValueError):
    """Подписка не той формы: адрес не службы пуша или ключи не те (ERR-PUSH-001)."""


class WebPushNotConfiguredError(RuntimeError):
    """Пуш выключен на этом контуре: composition root не положил адаптер."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """base64url без выравнивания, как его отдаёт браузер; мусор — `ValueError`."""
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError) as error:
        raise ValueError("not base64url") from error


def validate_push_endpoint(endpoint: str) -> str:
    """Адрес службы пуша из белого списка; иначе `InvalidPushSubscriptionError`."""
    if len(endpoint) > MAX_ENDPOINT_LENGTH or not endpoint.isprintable() or " " in endpoint:
        raise InvalidPushSubscriptionError("push endpoint is too long or not printable")
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or not (host in _EXACT_HOSTS or host.endswith(_HOST_SUFFIXES))
    ):
        raise InvalidPushSubscriptionError("push endpoint is not a known push service")
    return endpoint


def build_push_target(endpoint: str, p256dh: str, auth: str) -> PushTarget:
    """Подписка из того, что прислал браузер (`PushSubscription.toJSON()`), с проверкой."""
    validate_push_endpoint(endpoint)
    try:
        public_key = b64url_decode(p256dh)
        secret = b64url_decode(auth)
        if len(public_key) != _P256DH_LENGTH or len(secret) != _AUTH_LENGTH:
            raise ValueError("wrong key length")
        # Точка обязана лежать на кривой — иначе ECDH при отправке упал бы позже.
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key)
    except ValueError as error:
        raise InvalidPushSubscriptionError("push subscription keys are malformed") from error
    return PushTarget(endpoint=endpoint, p256dh=public_key, auth=secret)


class WebPushPort(Protocol):
    """Отправка веб-пуша.

    Контракт (общие тесты — `tests/test_web_push_port.py`):
    - `application_server_key` — публичный ключ отправителя, base64url точки P-256:
      браузер подписывается на него (`applicationServerKey`);
    - `send` не бросает на ответах службы пуша и сетевых сбоях — исход в
      `PushDelivery`; адрес вне белого списка — `InvalidPushSubscriptionError`,
      тело длиннее `MAX_PAYLOAD_BYTES` — `ValueError`, до любой сети;
    - `check` — health адаптера (§8 правило 1): ключ загружен.
    """

    backend: str
    application_server_key: str

    async def send(
        self, target: PushTarget, payload: bytes, *, ttl_seconds: int
    ) -> PushDelivery: ...

    async def check(self) -> bool: ...


_web_push: WebPushPort | None = None


def set_web_push(web_push: WebPushPort | None) -> None:
    """Положить адаптер процесса (`None` — пуш выключен). Зовёт composition root и тесты."""
    global _web_push
    _web_push = web_push


def web_push_or_none() -> WebPushPort | None:
    """Адаптер процесса; `None` — пуша на этом контуре нет (кнопки нет, рассылка молчит)."""
    return _web_push


def get_web_push() -> WebPushPort:
    """Адаптер процесса; пуш выключен — `WebPushNotConfiguredError`."""
    if _web_push is None:
        raise WebPushNotConfiguredError("web push is off: STAFF_PUSH_VAPID_PRIVATE_KEY is empty")
    return _web_push
