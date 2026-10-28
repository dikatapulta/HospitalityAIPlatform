"""Шифрование сообщения веб-пуша — RFC 8291 поверх RFC 8188 (`aes128gcm`).

Служба пуша (Google, Apple) видит только шифртекст: расшифровать его может
лишь браузер, у которого приватная пара к `p256dh` и секрет `auth`. Здесь —
одна запись на сообщение (RFC 8291 §4) и одноразовая пара ключей сервера на
каждое сообщение. Проверено тестовым вектором RFC 8291, приложение A, байт в
байт (`tests/test_web_push_port.py`).
"""

from __future__ import annotations

import os
import struct
from typing import Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from hospitality.shared.ports.web_push import MAX_PAYLOAD_BYTES, PushTarget

# Размер записи в заголовке (RFC 8188 §2.1); у одной записи он лишь обязан быть
# больше сообщения с разделителем и тегом (RFC 8291 §4).
_RECORD_SIZE: Final = 4096
# Разделитель последней записи (RFC 8188 §2): без него браузер сообщение выбросит.
_LAST_RECORD_DELIMITER: Final = b"\x02"


def _hkdf(*, salt: bytes, info: bytes, length: int, key_material: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(key_material)


def _uncompressed(public_key: ec.EllipticCurvePublicKey) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def encrypt_payload(
    target: PushTarget,
    payload: bytes,
    *,
    server_key: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Тело запроса к службе пуша: заголовок RFC 8188 и одна зашифрованная запись.

    `server_key` и `salt` передаёт только тест вектора RFC; в работе они свежие
    на каждое сообщение — повтор пары и соли раскрыл бы ключ записи.
    """
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"push payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    server_public = _uncompressed(server_key.public_key())
    browser_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), target.p256dh)
    shared_secret = server_key.exchange(ec.ECDH(), browser_key)
    # RFC 8291 §3.4: секрет ECDH и `auth` браузера сводятся в IKM…
    key_info = b"WebPush: info\x00" + target.p256dh + server_public
    input_key = _hkdf(salt=target.auth, info=key_info, length=32, key_material=shared_secret)
    # …а из IKM и соли — ключ и nonce записи (RFC 8188 §2.2, §2.3).
    content_key = _hkdf(
        salt=salt, info=b"Content-Encoding: aes128gcm\x00", length=16, key_material=input_key
    )
    nonce = _hkdf(salt=salt, info=b"Content-Encoding: nonce\x00", length=12, key_material=input_key)
    ciphertext = AESGCM(content_key).encrypt(nonce, payload + _LAST_RECORD_DELIMITER, None)
    header = salt + struct.pack("!L", _RECORD_SIZE) + bytes([len(server_public)]) + server_public
    return header + ciphertext
